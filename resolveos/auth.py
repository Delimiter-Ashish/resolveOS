"""Authentication: API keys (machines) and signed sessions (humans, multi-tenant), role scopes."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException, Query

from . import audit
from .config import settings
from .db import conn, hash_key

SCOPES: dict[str, set[str]] = {
    "agent": {"read", "write", "policy:check", "approval:request", "approval:read", "knowledge:read"},
    "approver": {"read", "approval:read", "approval:decide", "workflow:read", "audit:read", "knowledge:read"},
    "operator": {"read", "workflow:submit", "workflow:read", "audit:read", "approval:read", "knowledge:read",
                 "reply:send", "feedback:write"},
    "admin": {"*"},
}


@dataclass
class Principal:
    name: str
    role: str
    org: str = "default"
    kind: str = "key"  # key | session

    def can(self, scope: str) -> bool:
        s = SCOPES.get(self.role, set())
        return "*" in s or scope in s


# ---------------------------------------------------------------- passwords
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_password(pw: str, stored: str) -> bool:
    try:
        _, s, d = stored.split("$")
        dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(s), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(d))
    except Exception:
        return False


# ---------------------------------------------------------------- signed tokens
def _secret() -> bytes:
    if not settings.secret_key:
        raise HTTPException(500, {"error": "secret_key_not_configured", "hint": "run python -m resolveos.db init"})
    return settings.secret_key.encode()


def sign(payload: dict, prefix: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), f"{prefix}.{body}".encode(), hashlib.sha256).hexdigest()[:40]
    return f"{prefix}.{body}.{sig}"


def unsign(token: str, prefix: str) -> dict | None:
    try:
        p, body, sig = token.split(".")
        if p != prefix:
            return None
        good = hmac.new(_secret(), f"{p}.{body}".encode(), hashlib.sha256).hexdigest()[:40]
        if not hmac.compare_digest(good, sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def make_session(email: str, role: str, org: str) -> str:
    return sign({"u": email, "r": role, "o": org, "exp": time.time() + settings.session_hours * 3600}, "sess")


# ---------------------------------------------------------------- resolution
def resolve_token(token: str) -> Principal | None:
    if token.startswith("sess."):
        d = unsign(token, "sess")
        return Principal(d["u"], d["r"], d["o"], "session") if d else None
    with conn() as c:
        row = c.execute(
            "SELECT principal, role, org_id FROM api_keys WHERE key_hash=%s AND active", (hash_key(token),)
        ).fetchone()
    return Principal(row["principal"], row["role"], row["org_id"]) if row else None


def _extract(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def require(scope: str):
    """FastAPI dependency factory: authenticate (API key or session) and authorise for `scope`."""

    def dep(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
        x_workflow_id: str | None = Header(default=None),
    ) -> Principal:
        key = _extract(authorization, x_api_key)
        if not key:
            audit.safe_log(None, "anonymous", "auth.missing_key", {"scope": scope})
            raise HTTPException(401, {"error": "missing_credentials"})
        p = resolve_token(key)
        if not p:
            audit.safe_log(None, "anonymous", "auth.invalid_key", {"scope": scope})
            raise HTTPException(401, {"error": "invalid_credentials"})
        if not p.can(scope):
            audit.safe_log(x_workflow_id, p.name, "auth.forbidden", {"scope": scope, "role": p.role})
            raise HTTPException(403, {"error": "forbidden", "scope": scope, "role": p.role})
        return p

    return dep


def require_query_token(scope: str):
    """For EventSource / links that cannot send headers."""

    def dep(token: str = Query(...)) -> Principal:
        p = resolve_token(token)
        if not p or not p.can(scope):
            raise HTTPException(401, {"error": "invalid_credentials"})
        return p

    return dep


def org_of_workflow(c, workflow_id: str | None, default: str) -> str:
    if not workflow_id:
        return default
    row = c.execute("SELECT org_id FROM workflows WHERE id=%s", (workflow_id,)).fetchone()
    return row["org_id"] if row else default
