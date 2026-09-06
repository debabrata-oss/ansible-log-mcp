"""
HTTP/SSE transport for ansible-logs, with per-user Bearer-token auth.

Reuses all tools already defined in ansible_mcp.py (scan_logs, ping_hosts,
full_health_check, etc.) — this file only adds a network-reachable
transport and per-user auth in front of it.

Keys are loaded from users.yaml (see users.yaml.example) — one key per
teammate, each with a name for logging. This means:
  - You can revoke one person's access without changing everyone else's key
  - Every request is logged with who made it, not just "someone with the key"

Run:
    uvicorn ansible_mcp_http:app --host 0.0.0.0 --port 8787

Test locally:
    curl -H "Authorization: Bearer <a-users-key>" http://127.0.0.1:8787/sse
"""

import logging
import os
import secrets
from pathlib import Path

import yaml
from starlette.responses import JSONResponse

from mcp.server.transport_security import TransportSecuritySettings

from ansible_mcp import mcp  # noqa: F401 — imports and registers all @mcp.tool() functions

# FastMCP's SSE transport rejects any request whose Host header isn't in
# an allowlist (DNS-rebinding protection) — by default only
# 127.0.0.1/localhost/[::1], which is why a request via the LAN IP gets
# "Invalid Host header". Extend it to cover every hostname/IP this
# server is actually meant to be reached at. Set ANSIBLE_MCP_ALLOWED_HOSTS
# (comma-separated, e.g. "192.168.1.2:8787,my-internal-domain:443") to
# override without editing code.
_default_hosts = "127.0.0.1:8787,localhost:8787,192.168.1.2:8787"
_allowed_hosts = os.environ.get("ANSIBLE_MCP_ALLOWED_HOSTS", _default_hosts).split(",")

mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_allowed_hosts,
    allowed_origins=["*"],  # Origin header isn't meaningful for non-browser MCP clients
)

USERS_FILE = Path(os.environ.get("ANSIBLE_MCP_USERS_FILE", Path(__file__).parent.parent / "users.yaml"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ansible-mcp-auth")


def load_users() -> dict:
    """
    Load {api_key: username} from users.yaml. Expected format:

        users:
          - name: debabrata
            api_key: <long-random-string>
          - name: priya
            api_key: <a-different-long-random-string>
    """
    if not USERS_FILE.exists():
        raise RuntimeError(
            f"Users file not found at {USERS_FILE}. Create it — see users.yaml.example "
            f"for the format — or set ANSIBLE_MCP_USERS_FILE to point elsewhere."
        )
    with open(USERS_FILE) as f:
        data = yaml.safe_load(f) or {}

    users = data.get("users") or []
    key_to_name = {}
    for u in users:
        name = u.get("name")
        key = u.get("api_key")
        if not name or not key:
            raise RuntimeError(f"Invalid entry in {USERS_FILE}: {u} — each user needs name and api_key")
        key_to_name[key] = name

    if not key_to_name:
        raise RuntimeError(f"{USERS_FILE} has no users defined — add at least one.")
    return key_to_name


USERS = load_users()  # {api_key: name}


class PerUserApiKeyMiddleware:
    """
    Requires 'Authorization: Bearer <key>' on every HTTP request, where
    <key> must match one of the keys in users.yaml. Logs which user made
    each request (by name, not by raw key) for basic auditability.

    Raw ASGI middleware (not Starlette's BaseHTTPMiddleware) because
    BaseHTTPMiddleware doesn't handle long-lived SSE streams well —
    see the note in the original single-key version this replaced.
    """

    def __init__(self, app, users: dict):
        self.app = app
        self.users = users

    _UNAUTHENTICATED_PATHS = (
        "/.well-known/",
        "/register",
    )

    def _lookup_user(self, presented_key: str) -> str | None:
        """Constant-time-ish lookup: compare against every known key
        rather than short-circuiting, to avoid leaking which prefix
        matched via timing. With a handful of users this cost is trivial."""
        matched = None
        for key, name in self.users.items():
            if secrets.compare_digest(presented_key, key):
                matched = name
        return matched

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if any(path.startswith(p) for p in self._UNAUTHENTICATED_PATHS):
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode()

        presented_key = ""
        if auth_header.startswith("Bearer "):
            presented_key = auth_header[len("Bearer "):]

        user = self._lookup_user(presented_key) if presented_key else None

        if not user:
            logger.warning("Rejected request to %s: invalid or missing API key", path)
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        logger.info("Request to %s from user=%s", path, user)
        scope["state"] = scope.get("state", {})
        scope["state"]["user"] = user
        await self.app(scope, receive, send)


app = PerUserApiKeyMiddleware(mcp.sse_app(), USERS)