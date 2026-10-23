"""Authenticated local web application. Run behind TLS for any remote deployment."""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..models import Rejected, canonical, digest
from .catalog import Catalog
from .db import DB, decoded
from .environments import Environments
from .isc import ISC
from .jobs import Worker
from .model_profiles import ModelProfiles
from .llm import PROVIDERS
from .network import HTTP
from .research import Research, OFFICIAL_HOSTS
from .security import Vault, authenticate, authorize, password_hash, restricted_write, session, public_url
from .specialists import ToolRegistry
from .workflows import Workflows


@dataclass
class Settings:
    directory: str = "work/platform-v2"
    worker: bool = True
    auto_ingestion: bool = True
    bootstrap_password: str | None = None
    secure_cookie: bool = False
    allowed_hosts: tuple = ("localhost", "127.0.0.1", "[::1]")


class Context:
    def __init__(self, settings, http=None):
        self.db = DB(Path(settings.directory) / "platform.db")
        self.vault = Vault(settings.directory)
        self.http = http or HTTP()
        self.envs = Environments(self.db, self.vault)
        self.models = ModelProfiles(self.db, self.vault)
        self.catalog = Catalog(self.db)
        self.workflows = Workflows(self.db, self.envs, self.isc)
        self.research = Research(self.db, self.http, os.environ.get("BRAVE_SEARCH_API_KEY"),
                                 os.environ.get("ISC_RESEARCH_HOSTS", "").split(","))
        self.user = None

    def isc(self, eid):
        return ISC(self.db, self.envs, self.http, eid)

    def close(self):
        self.http.close()
        self.db.close()


def create_app(settings=None):
    settings = settings or Settings()
    directory = Path(settings.directory)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    initial = Context(settings)
    try:
        if not initial.db.one("SELECT 1 FROM users LIMIT 1"):
            password = settings.bootstrap_password or os.environ.get("ISC_BOOTSTRAP_PASSWORD") or secrets.token_urlsafe(24)
            initial.db.execute("INSERT INTO users VALUES(?,?,?)", ("admin", password_hash(password), "admin"))
            if not settings.bootstrap_password and not os.environ.get("ISC_BOOTSTRAP_PASSWORD"):
                restricted_write(directory / "bootstrap-password.txt", password.encode())
        if settings.auto_ingestion:
            initial.db.execute("INSERT OR IGNORE INTO schedules VALUES(?,?,?,?,?,?)",
                ("official-spec-daily", "sync_specs", None, "{}", 86400, time.time()))
    finally:
        initial.close()

    stop = threading.Event()
    def worker_loop():
        ctx = Context(settings)
        try:
            worker = Worker(ctx.db, ctx.envs, ctx.http)
            while not stop.is_set():
                try:
                    worked = worker.run_one()
                except Exception:
                    worked = False
                stop.wait(0.1 if worked else 1)
        finally:
            ctx.close()

    @asynccontextmanager
    async def lifespan(app):
        thread = None
        if settings.worker:
            thread = threading.Thread(target=worker_loop, daemon=True, name="isc-workflow-worker")
            thread.start()
        yield
        stop.set()
        if thread:
            thread.join(timeout=2)

    app = FastAPI(title="ISC Integration Studio", version="0.3.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    @app.middleware("http")
    async def security_headers(request, call_next):
        if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > 8_000_000:
            return JSONResponse({"error": "Request exceeds the 8 MB budget"}, status_code=413)
        # Limit chunked requests too, without retaining a second copy of the body.
        received = 0
        original_receive = request._receive
        async def bounded_receive():
            nonlocal received
            message = await original_receive()
            received += len(message.get("body", b""))
            if received > 8_000_000:
                raise Rejected("Request exceeds the 8 MB budget")
            return message
        request._receive = bounded_receive
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if settings.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(Rejected)
    async def rejected(request, exc):
        status = 401 if str(exc) == "Authentication required" else 400
        return JSONResponse({"error": str(exc)}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"error": "Invalid request structure"}, status_code=422)

    def context(request: Request):
        ctx = Context(settings)
        try:
            ctx.user = session(ctx.db, request.cookies.get("isc_session"))
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), ctx.user["csrf"]):
                    raise Rejected("CSRF token missing or invalid")
                origin = request.headers.get("Origin")
                if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                    raise Rejected("Cross-origin request rejected")
            yield ctx
        finally:
            ctx.close()

    def env_access(ctx, eid, *, admin=False, mutate=False, review=False):
        authorize(ctx.db, ctx.user, eid, admin=admin)
        if mutate and ctx.user["role"] == "reviewer":
            raise Rejected("Reviewers cannot initiate execution or create plans")
        if review and ctx.user["role"] not in {"admin", "reviewer"}:
            raise Rejected("A reviewer role is required for approval")
        return ctx.envs.get(eid)

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "static/index.html")

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.post("/api/login")
    def login(request: Request, payload: dict, response: Response):
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise Rejected("Cross-origin login rejected")
        ctx = Context(settings)
        try:
            username, password = payload.get("username", ""), payload.get("password", "")
            if not isinstance(username, str) or not isinstance(password, str) or len(username) > 100:
                raise Rejected("Invalid login fields")
            token, csrf, user = authenticate(ctx.db, username, password, request.client.host)
            response.set_cookie("isc_session", token, httponly=True, secure=settings.secure_cookie, samesite="strict", max_age=28800)
            return {"user": user, "csrf": csrf}
        finally:
            ctx.close()

    @app.get("/api/me")
    def me(ctx=Depends(context)):
        error = None
        try:
            selected_model = ctx.models.snapshot(ctx.user["user_id"])
        except Rejected as exc:
            selected_model, error = None, str(exc)
        return {"user": {"id": ctx.user["user_id"], "role": ctx.user["role"]}, "csrf": ctx.user["csrf"],
                "model_configured": bool(selected_model), "selected_model": selected_model, "model_error": error,
                "search_configured": bool(os.environ.get("BRAVE_SEARCH_API_KEY")),
                "model": selected_model["model"] if selected_model else None}

    @app.get("/api/model-profiles")
    def model_profiles(ctx=Depends(context)):
        try:
            server = ctx.models.snapshot(ctx.user["user_id"], "")
        except Rejected:
            server = None
        return {"profiles": ctx.models.list(ctx.user["user_id"]), "selected_id": ctx.models.selected_id(ctx.user["user_id"]),
                "server_default": server, "providers": [{"id": k, "label": v["label"]} for k, v in PROVIDERS.items()]}

    @app.post("/api/model-profiles")
    def create_model_profile(payload: dict, ctx=Depends(context)):
        return ctx.models.save(ctx.user["user_id"], payload)

    @app.put("/api/model-profiles/{pid}")
    def update_model_profile(pid: str, payload: dict, ctx=Depends(context)):
        return ctx.models.save(ctx.user["user_id"], payload, pid)

    @app.delete("/api/model-profiles/{pid}")
    def delete_model_profile(pid: str, ctx=Depends(context)):
        ctx.models.delete(ctx.user["user_id"], pid)
        return {"ok": True}

    @app.post("/api/model-selection")
    def select_model(payload: dict, ctx=Depends(context)):
        profile_id = payload.get("profile_id")
        if profile_id is not None and not isinstance(profile_id, str):
            raise Rejected("Choose a saved model profile")
        ctx.models.select(ctx.user["user_id"], profile_id)
        return {"selected_model": ctx.models.snapshot(ctx.user["user_id"])}

    @app.post("/api/model-profiles/{pid}/test")
    def test_model_profile(pid: str, ctx=Depends(context)):
        snapshot = ctx.models.snapshot(ctx.user["user_id"], pid)
        model = ctx.models.model(ctx.http, ctx.user["user_id"], snapshot)
        result = model.generate("Connection test: return exactly the requested JSON object with ok=true",
            {"task": "Return a JSON object with ok=true. No tenant data is supplied and no tools should be called."},
            {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False})
        if result != {"ok": True}:
            raise Rejected("The server responded but failed the structured-output test")
        with ctx.db.tx():
            ctx.db.audit(ctx.user["user_id"], None, "model_profile.tested", {"id": pid, "provider": snapshot["provider"], "model": snapshot["model"]})
        return {"ok": True, "provider": snapshot["provider"], "model": snapshot["model"], "message": "Connected; structured JSON output validated"}

    @app.post("/api/logout")
    def logout(response: Response, ctx=Depends(context)):
        ctx.db.execute("DELETE FROM sessions WHERE id=?", (ctx.user["id"],))
        response.delete_cookie("isc_session")
        return {"ok": True}

    @app.post("/api/users")
    def add_user(payload: dict, ctx=Depends(context)):
        authorize(ctx.db, ctx.user, None, admin=True)
        name, role = payload.get("username", ""), payload.get("role")
        if not isinstance(name, str) or not name.isidentifier() or len(name) > 64 or role not in {"admin", "operator", "reviewer"}:
            raise Rejected("Invalid username or role")
        if ctx.db.one("SELECT 1 FROM users WHERE id=?", (name,)):
            raise Rejected("User already exists")
        encoded = password_hash(payload.get("password", ""))
        grants = payload.get("environments", [])
        for eid in grants:
            ctx.envs.get(eid)
        with ctx.db.tx():
            ctx.db.execute("INSERT INTO users VALUES(?,?,?)", (name, encoded, role))
            for eid in grants:
                ctx.db.execute("INSERT INTO grants VALUES(?,?)", (name, eid))
            ctx.db.audit(ctx.user["user_id"], None, "user.created", {"user": name, "role": role, "grants": grants})
        return {"id": name, "role": role}

    @app.get("/api/environments")
    def environments(ctx=Depends(context)):
        rows = ctx.envs.list()
        if ctx.user["role"] != "admin":
            grants = {r["environment_id"] for r in ctx.db.all("SELECT environment_id FROM grants WHERE user_id=?", (ctx.user["user_id"],))}
            rows = [r for r in rows if r["id"] in grants]
        return rows

    @app.post("/api/environments")
    def create_environment(payload: dict, ctx=Depends(context)):
        authorize(ctx.db, ctx.user, None, admin=True)
        return ctx.envs.save(payload, ctx.user["user_id"])

    @app.put("/api/environments/{eid}")
    def save_environment(eid: str, payload: dict, ctx=Depends(context)):
        env_access(ctx, eid, admin=True)
        return ctx.envs.save(payload, ctx.user["user_id"], eid)

    @app.get("/api/specs")
    def specs(ctx=Depends(context)):
        return ctx.catalog.list()

    @app.post("/api/specs/sync")
    def sync_specs(ctx=Depends(context)):
        authorize(ctx.db, ctx.user, None, admin=True)
        return {"job_id": ctx.db.enqueue("sync_specs", None, {})}

    @app.post("/api/ingestion")
    def ingestion(payload: dict, ctx=Depends(context)):
        authorize(ctx.db, ctx.user, None, admin=True)
        seeds = payload.get("seeds", [])
        if not isinstance(seeds, list) or not 1 <= len(seeds) <= 10 or not all(isinstance(s, str) for s in seeds):
            raise Rejected("Provide 1–10 official documentation URLs")
        for seed in seeds:
            public_url(seed, OFFICIAL_HOSTS)
        pages = payload.get("max_pages", 50)
        if type(pages) is not int or not 1 <= pages <= 500:
            raise Rejected("Page budget must be 1–500")
        state = {"seeds": seeds, "max_pages": pages}
        with ctx.db.tx():
            job_id = ctx.db.enqueue("crawl", None, state)
            if payload.get("daily"):
                ctx.db.execute("INSERT OR REPLACE INTO schedules VALUES(?,?,?,?,?,?)",
                    (digest(seeds), "crawl", None, canonical(state), 86400, time.time() + 86400))
            ctx.db.audit(ctx.user["user_id"], None, "crawl.requested", {"job": job_id, "seeds": seeds, "pages": pages})
        return {"job_id": job_id}

    @app.get("/api/jobs")
    def jobs(ctx=Depends(context)):
        sql = "SELECT id,kind,environment_id,state,result,error,created,updated FROM jobs"
        args = ()
        if ctx.user["role"] != "admin":
            sql += " WHERE environment_id IN (SELECT environment_id FROM grants WHERE user_id=?)"
            args = (ctx.user["user_id"],)
        return [decoded(r, "result") for r in ctx.db.all(sql + " ORDER BY created DESC LIMIT 100", args)]

    @app.get("/api/environments/{eid}/operations")
    def operations(eid: str, query: str = "", operation_id: str = "", ctx=Depends(context)):
        env = env_access(ctx, eid)
        return ctx.catalog.describe(env["spec_id"], operation_id) if operation_id else ctx.catalog.operations(env["spec_id"], query)

    @app.post("/api/environments/{eid}/discover")
    def discover(eid: str, ctx=Depends(context)):
        env = env_access(ctx, eid)
        if env["backend"] != "isc":
            raise Rejected("Discovery requires a live ISC environment")
        return {"job_id": ctx.db.enqueue("discover", eid, {})}

    @app.post("/api/environments/{eid}/attachments")
    def attachment(eid: str, payload: dict, ctx=Depends(context)):
        env_access(ctx, eid, mutate=True)
        return ctx.envs.attach(eid, payload.get("name", ""), payload.get("base64", ""), ctx.user["user_id"])

    @app.get("/api/environments/{eid}/attachments")
    def attachments(eid: str, ctx=Depends(context)):
        env_access(ctx, eid)
        return ctx.db.all("SELECT id,name,size,hash FROM attachments WHERE environment_id=?", (eid,))

    @app.post("/api/environments/{eid}/attachments/{fid}/validate")
    def validate_attachment(eid: str, fid: str, payload: dict, ctx=Depends(context)):
        env_access(ctx, eid)
        return ctx.envs.validate_csv(eid, fid, payload.get("account_id"), payload.get("required_attributes", []))

    @app.post("/api/environments/{eid}/plan")
    def plan(eid: str, payload: dict, ctx=Depends(context)):
        environment = env_access(ctx, eid, mutate=True)
        prompt = payload.get("prompt", "")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 12000 or type(payload.get("external", False)) is not bool:
            raise Rejected("Provide a bounded prompt and boolean research consent")
        if payload.get("model_profile_id") is not None and not isinstance(payload["model_profile_id"], str):
            raise Rejected("Choose a saved model profile")
        snapshot = None
        if environment["backend"] == "isc" and not payload.get("proposal"):
            snapshot = ctx.models.snapshot(ctx.user["user_id"], payload.get("model_profile_id"))
            if not snapshot:
                raise Rejected("Choose a model in Models & appearance. Local Qwen or Llama through llama.cpp needs no OpenAI account")
        job_id = ctx.db.enqueue("plan", eid, {"prompt": prompt, "external": payload.get("external", False),
            "requirements": payload.get("requirements"), "proposal": payload.get("proposal"), "actor": ctx.user["user_id"], "model_profile": snapshot})
        return {"job_id": job_id}

    @app.get("/api/environments/{eid}/runs")
    def runs(eid: str, ctx=Depends(context)):
        env_access(ctx, eid)
        return ctx.db.all("SELECT id,creator,status,hash,created,error FROM live_runs WHERE environment_id=? ORDER BY created DESC LIMIT 100", (eid,))

    @app.get("/api/environments/{eid}/runs/{rid}")
    def run(eid: str, rid: str, ctx=Depends(context)):
        env_access(ctx, eid)
        return ctx.workflows.get(rid, eid)

    @app.post("/api/environments/{eid}/runs/{rid}/approve")
    def approve(eid: str, rid: str, payload: dict, ctx=Depends(context)):
        env_access(ctx, eid, review=True)
        return ctx.workflows.approve(rid, eid, payload.get("hash"), ctx.user["user_id"])

    @app.post("/api/environments/{eid}/runs/{rid}/revoke")
    def revoke(eid: str, rid: str, ctx=Depends(context)):
        env_access(ctx, eid, review=True)
        with ctx.db.tx():
            run = ctx.workflows.get(rid, eid)
            if run["status"] not in {"approved", "running"} or any(s["state"] == "sending" for s in run["steps"]):
                raise Rejected("Cannot revoke while a remote step is in flight; inspect/reconcile first")
            ctx.db.execute("UPDATE live_runs SET status='planned',approval=NULL WHERE id=?", (rid,))
            ctx.db.execute("UPDATE jobs SET state='cancelled' WHERE kind='execute' AND environment_id=? AND json_extract(payload,'$.run_id')=? AND state='queued'", (eid, rid))
            ctx.db.audit(ctx.user["user_id"], eid, "run.approval_revoked", {"run": rid})
        return ctx.workflows.get(rid, eid)

    @app.post("/api/environments/{eid}/runs/{rid}/execute")
    def execute(eid: str, rid: str, ctx=Depends(context)):
        env = env_access(ctx, eid, mutate=True)
        with ctx.db.tx():
            run = ctx.workflows.get(rid, eid)
            if run["status"] not in {"approved", "running"}:
                raise Rejected("Approve the run before execution")
            ctx.workflows._check(run, env)
            pending = ctx.db.one("SELECT id FROM jobs WHERE kind='execute' AND environment_id=? AND json_extract(payload,'$.run_id')=? AND state IN ('queued','running')", (eid, rid))
            job_id = pending["id"] if pending else ctx.db.enqueue("execute", eid, {"run_id": rid})
            ctx.db.audit(ctx.user["user_id"], eid, "run.execution_requested", {"run": rid, "job": job_id})
        return {"job_id": job_id}

    @app.post("/api/environments/{eid}/runs/{rid}/reconcile")
    def reconcile(eid: str, rid: str, payload: dict, ctx=Depends(context)):
        env_access(ctx, eid, admin=True)
        return ctx.workflows.reconcile(rid, eid, ctx.user["user_id"], operation_id=payload.get("operation_id"),
            arguments=payload.get("arguments", {}), expected=payload.get("expected"), decision=payload.get("decision"))

    @app.post("/api/environments/{eid}/knowledge/search")
    def search(eid: str, payload: dict, ctx=Depends(context)):
        env_access(ctx, eid)
        return ctx.research.search(payload.get("query", ""), eid)

    @app.post("/api/environments/{eid}/knowledge/import")
    def import_knowledge(eid: str, payload: dict, ctx=Depends(context)):
        env_access(ctx, eid, admin=True)
        content, title = payload.get("content", ""), payload.get("title", "")
        if not isinstance(content, str) or not content.strip() or len(content) > 500000 or not isinstance(title, str):
            raise Rejected("Provide a title and at most 500 KB of document text")
        return ctx.research.ingest("client://" + uuid_hex(), content, title, "client", scope=eid)

    @app.post("/api/environments/{eid}/research")
    def research(eid: str, payload: dict, ctx=Depends(context)):
        env_access(ctx, eid)
        if payload.get("external") is not True:
            raise Rejected("Explicit external-research consent required")
        return ctx.research.fetch(payload["url"]) if payload.get("url") else ctx.research.web_search(payload.get("query", ""))

    @app.get("/api/environments/{eid}/tools")
    def tools(eid: str, ctx=Depends(context)):
        env_access(ctx, eid)
        return [decoded(r, "manifest") for r in ctx.db.all("SELECT * FROM tools WHERE environment_id=?", (eid,))]

    @app.post("/api/environments/{eid}/tools")
    def propose_tool(eid: str, payload: dict, ctx=Depends(context)):
        env = env_access(ctx, eid, mutate=True)
        return ToolRegistry(ctx.db, env).propose(payload, ctx.user["user_id"])

    @app.post("/api/environments/{eid}/tools/{tid}/activate")
    def activate_tool(eid: str, tid: str, payload: dict, ctx=Depends(context)):
        env = env_access(ctx, eid, admin=True)
        return ToolRegistry(ctx.db, env).activate(tid, payload.get("hash"), ctx.user["user_id"])

    @app.post("/api/environments/{eid}/tools/{tid}/deactivate")
    def deactivate_tool(eid: str, tid: str, ctx=Depends(context)):
        env_access(ctx, eid, admin=True)
        with ctx.db.tx():
            row = ctx.db.one("SELECT id FROM tools WHERE id=? AND environment_id=?", (tid, eid))
            if not row:
                raise Rejected("Tool not found")
            ctx.db.execute("UPDATE tools SET status='inactive' WHERE id=?", (tid,))
            ctx.db.audit(ctx.user["user_id"], eid, "tool.deactivated", {"id": tid})
        return {"id": tid, "status": "inactive"}

    @app.get("/api/environments/{eid}/drafts")
    def drafts(eid: str, ctx=Depends(context)):
        env_access(ctx, eid)
        return [decoded(r, "review") for r in ctx.db.all("SELECT * FROM drafts WHERE environment_id=?", (eid,))]

    @app.get("/api/environments/{eid}/audit")
    def audit(eid: str, ctx=Depends(context)):
        env_access(ctx, eid)
        return [decoded(r, "details") for r in ctx.db.all("SELECT * FROM audit WHERE environment_id=? ORDER BY seq DESC LIMIT 300", (eid,))]

    return app


def uuid_hex():
    import uuid
    return uuid.uuid4().hex


def main(argv=None):
    parser = argparse.ArgumentParser(description="ISC Integration Studio")
    parser.add_argument("command", choices=["serve", "worker", "sync-specs", "init"], nargs="?", default="serve")
    parser.add_argument("--data", default="work/platform-v2")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    secure = os.environ.get("ISC_PUBLIC_ORIGIN", "").startswith("https://")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not secure:
        parser.error("Remote binding requires ISC_PUBLIC_ORIGIN=https://... and a TLS reverse proxy")
    settings = Settings(directory=args.data, secure_cookie=secure,
        allowed_hosts=tuple(os.environ.get("ISC_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")))
    app = create_app(settings)
    if args.command == "serve":
        import uvicorn
        print(f"ISC Integration Studio: http://{args.host}:{args.port}")
        print(f"Initial admin password, when generated: {args.data}/bootstrap-password.txt (owner-read only)")
        uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    elif args.command == "init":
        print(f"Initialized {args.data}. See README for admin authentication and secret management.")
    else:
        ctx = Context(settings)
        try:
            worker = Worker(ctx.db, ctx.envs, ctx.http)
            if args.command == "sync-specs":
                job_id = ctx.db.enqueue("sync_specs", None, {})
                while ctx.db.one("SELECT state FROM jobs WHERE id=?", (job_id,))["state"] not in {"completed", "failed"}:
                    worker.run_one()
                print(canonical(decoded(ctx.db.one("SELECT id,state,result,error FROM jobs WHERE id=?", (job_id,)), "result")))
            else:
                while True:
                    worked = worker.run_one()
                    if args.once:
                        break
                    if not worked:
                        time.sleep(1)
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
