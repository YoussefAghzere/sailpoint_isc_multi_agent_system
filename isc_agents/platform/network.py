"""Bounded HTTPS access; redirects never inherit credentials."""
import time
import httpx

from ..models import Rejected


class Ambiguous(Rejected):
    """A remote mutation might have happened. Never retry automatically."""


class HTTP:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=httpx.Timeout(40, connect=10), follow_redirects=False)
        self.owned = client is None

    def close(self):
        if self.owned:
            self.client.close()

    def request(self, method, url, *, max_bytes=8_000_000, before_attempt=None, **kwargs):
        read_only = method.upper() in {"GET", "HEAD"}
        for attempt in range(3 if read_only else 1):
            if before_attempt:
                before_attempt()
            try:
                with self.client.stream(method, url, **kwargs) as response:
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            error = Rejected if read_only else Ambiguous
                            raise error("Remote response exceeded the configured size budget")
                        chunks.append(chunk)
                    decoded_headers = {k: v for k, v in response.headers.items()
                                       if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}}
                    result = httpx.Response(response.status_code, headers=decoded_headers,
                                            content=b"".join(chunks), request=response.request)
            except httpx.RequestError:
                if read_only and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                error = Rejected if read_only else Ambiguous
                raise error("Remote request failed; check egress and service connectivity") from None
            if result.status_code in {429, 502, 503, 504} and read_only and attempt < 2:
                retry = result.headers.get("Retry-After", "1")
                time.sleep(min(float(retry), 3) if retry.isdigit() else 1)
                continue
            if not read_only and result.status_code >= 500:
                raise Ambiguous("Remote server error; reconcile before another mutation")
            if 300 <= result.status_code < 400 and result.status_code != 304:
                raise Rejected("Redirect rejected; configure the canonical destination")
            return result
        raise Rejected("Request budget exhausted")

    @staticmethod
    def require_ok(response):
        if response.status_code >= 400:
            raise Rejected(f"Remote service returned HTTP {response.status_code}")
        return response
