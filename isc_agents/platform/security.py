import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from ..models import Rejected, canonical


def restricted_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(content)


class Vault:
    def __init__(self, directory, key=None):
        path = Path(directory) / "vault.key"
        key = key or os.environ.get("ISC_MASTER_KEY")
        if key is None:
            if not path.exists():
                try:
                    restricted_write(path, Fernet.generate_key())
                except FileExistsError:
                    pass
            key = path.read_bytes()
        self.fernet = Fernet(key.encode() if isinstance(key, str) else key)
        self.fingerprint_key = hashlib.sha256(b"isc-state-fingerprint:" + (key.encode() if isinstance(key, str) else key)).digest()

    def encrypt(self, value):
        return self.fernet.encrypt(canonical(value).encode()).decode()

    def decrypt(self, value):
        try:
            return json.loads(self.fernet.decrypt(value.encode()))
        except (InvalidToken, ValueError):
            raise Rejected("Credential store cannot be decrypted with the configured master key") from None

    def fingerprint(self, value):
        return hmac.new(self.fingerprint_key, canonical(value).encode(), hashlib.sha256).hexdigest()


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise Rejected("Password must contain 12–1024 characters")
    salt = salt or secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()
    return salt + ":" + value


def authenticate(db, username, password, address):
    key, at = hashlib.sha256((username + address).encode()).hexdigest(), time.time()
    limit = db.one("SELECT * FROM login_limits WHERE id=?", (key,))
    if limit and limit["until"] > at and limit["failures"] >= 5:
        raise Rejected("Too many login attempts; retry after 15 minutes")
    user = db.one("SELECT * FROM users WHERE id=?", (username,))
    reference = user["password"] if user else "dummy:" + "0" * 64
    candidate = password_hash(password, reference.split(":")[0]) if 12 <= len(password) <= 1024 else "invalid"
    if not user or not hmac.compare_digest(candidate, reference):
        failures = limit["failures"] + 1 if limit and limit["until"] > at else 1
        db.execute("INSERT OR REPLACE INTO login_limits VALUES(?,?,?)", (key, failures, at + 900))
        raise Rejected("Invalid username or password")
    db.execute("DELETE FROM login_limits WHERE id=?", (key,))
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions VALUES(?,?,?,?)",
               (hashlib.sha256(token.encode()).hexdigest(), username, csrf, at + 8 * 3600))
    return token, csrf, {"id": user["id"], "role": user["role"]}


def session(db, token):
    if not token:
        raise Rejected("Authentication required")
    row = db.one("SELECT sessions.*, users.role FROM sessions JOIN users ON users.id=sessions.user_id "
                 "WHERE sessions.id=? AND expires>?", (hashlib.sha256(token.encode()).hexdigest(), time.time()))
    if not row:
        raise Rejected("Authentication required")
    return row


def authorize(db, user, environment_id, admin=False):
    if admin and user["role"] != "admin":
        raise Rejected("Administrator role required")
    if user["role"] != "admin" and not db.one("SELECT 1 FROM grants WHERE user_id=? AND environment_id=?",
                                              (user["user_id"], environment_id)):
        raise Rejected("Environment access denied")


def tenant_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (ValueError, TypeError):
        raise Rejected("Invalid tenant URL") from None
    suffixes = tuple(os.environ.get("ISC_TENANT_SUFFIXES", "api.identitynow.com").split(","))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or port or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or not any(parsed.hostname.endswith("." + s.strip()) for s in suffixes)
            or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname)):
        raise Rejected("Base URL must be an approved HTTPS ISC tenant origin without a path or port")
    return "https://" + parsed.hostname


def public_url(url, allowed_hosts):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (ValueError, TypeError):
        raise Rejected("Invalid research URL") from None
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or port or not host:
        raise Rejected("Research URLs must be HTTPS origins without credentials or ports")
    try:
        ipaddress.ip_address(host)
        raise Rejected("IP address destinations are forbidden")
    except ValueError:
        pass
    if host not in allowed_hosts or host in {"localhost", "metadata.google.internal"}:
        raise Rejected("Destination is outside the configured internet allowlist")
    return url


SECRET_KEYS = re.compile(r"password|secret|token|private.?key|api.?key|authorization|credential", re.I)


def redact(value, secrets_to_mask=()):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SECRET_KEYS.search(key) else redact(v, secrets_to_mask)
                for key, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets_to_mask) for v in value]
    if isinstance(value, str):
        for secret in secrets_to_mask:
            if isinstance(secret, str) and len(secret) >= 4:
                value = value.replace(secret, "[REDACTED]")
        return value
    return value


def reject_inline_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEYS.search(key) and item not in (None, ""):
                if not isinstance(item, str) or not re.fullmatch(r"\$\{secrets\.[A-Za-z0-9_-]+\}", item):
                    raise Rejected("Use environment secret references instead of inline credentials")
            reject_inline_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_inline_secrets(item)
