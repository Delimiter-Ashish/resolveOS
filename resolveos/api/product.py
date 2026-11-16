"""Product API: accounts & orgs, settings, autonomy, policy rules + what-if, connectors,
knowledge, replies, feedback/regression, intake channels, one-tap approvals, ROI, costs,
alerts and Prometheus metrics."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import audit, autonomy, knowledge, policy, replies, rules
from ..auth import (Principal, check_password, hash_password, make_session, org_of_workflow, require,
                    resolve_token, unsign)
from ..config import settings
from ..connectors.openapi import parse as parse_openapi
from ..db import conn
from ..tickets import create_ticket, new_id

router = APIRouter()
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def _admin(p: Principal) -> None:
    if p.role != "admin":
        raise HTTPException(403, {"error": "admin_only"})


# ================================================================ auth & orgs
class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/v1/auth/login")
def login(body: LoginIn):
    with conn() as c:
        u = c.execute("SELECT * FROM users WHERE lower(email)=lower(%s) AND active", (body.email.strip(),)).fetchone()
    if not u or not check_password(body.password, u["pw_hash"]):
        time.sleep(0.4)
        audit.safe_log(None, "anonymous", "auth.login_failed", {"email": body.email[:80]})
        raise HTTPException(401, {"error": "invalid_login"})
    audit.safe_log(None, u["email"], "auth.login", {"org": u["org_id"]})
    return {"token": make_session(u["email"], u["role"], u["org_id"]),
            "user": {"email": u["email"], "name": u["name"], "role": u["role"], "org": u["org_id"]}}


class SignupIn(BaseModel):
    org_name: str = Field(min_length=2, max_length=80)
    email: str
    password: str = Field(min_length=8)
    name: str = ""


@router.post("/v1/auth/signup", status_code=201)
def signup(body: SignupIn):
    if not settings.allow_signup:
        raise HTTPException(403, {"error": "signup_disabled"})
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", body.email):
        raise HTTPException(422, {"error": "invalid_email"})
    org = re.sub(r"[^a-z0-9]+", "-", body.org_name.lower()).strip("-")[:24] + "-" + secrets.token_hex(2)
    with conn() as c:
        if c.execute("SELECT 1 AS x FROM users WHERE lower(email)=lower(%s)", (body.email,)).fetchone():
            raise HTTPException(409, {"error": "email_taken"})
        c.execute("INSERT INTO orgs(id, name) VALUES (%s,%s)", (org, body.org_name))
        c.execute("INSERT INTO org_settings(org_id, widget_token) VALUES (%s,%s)", (org, "wt_" + secrets.token_urlsafe(18)))
        c.execute("INSERT INTO users(org_id, email, name, role, pw_hash) VALUES (%s,%s,%s,'admin',%s)",
                  (org, body.email, body.name, hash_password(body.password)))
    return {"org": org, "token": make_session(body.email, "admin", org)}


@router.get("/v1/me")
def me(p: Principal = Depends(require("read"))):
    with conn() as c:
        o = c.execute("SELECT name FROM orgs WHERE id=%s", (p.org,)).fetchone()
    return {"principal": p.name, "role": p.role, "org": p.org, "org_name": o["name"] if o else p.org, "kind": p.kind}


class UserIn(BaseModel):
    email: str
    password: str = Field(min_length=8)
    role: str = Field(pattern="^(approver|operator|admin)$")
    name: str = ""


@router.get("/v1/users")
def users(p: Principal = Depends(require("read"))):
    _admin(p)
    with conn() as c:
        rows = c.execute("SELECT id, email, name, role, active, created_at FROM users WHERE org_id=%s ORDER BY id", (p.org,)).fetchall()
    return {"users": jsonable_encoder(rows)}


@router.post("/v1/users", status_code=201)
def add_user(body: UserIn, p: Principal = Depends(require("read"))):
    _admin(p)
    with conn() as c:
        try:
            c.execute("INSERT INTO users(org_id, email, name, role, pw_hash) VALUES (%s,%s,%s,%s,%s)",
                      (p.org, body.email, body.name, body.role, hash_password(body.password)))
        except Exception:
            raise HTTPException(409, {"error": "email_taken"})
        audit.log(c, None, p.name, "user.created", {"email": body.email, "role": body.role, "org": p.org})
    return {"ok": True}


# ================================================================ settings & autonomy
SETTING_FIELDS = {"autonomy_mode", "trust_threshold", "trust_min_samples", "brand_tone", "reply_approval",
                  "handle_minutes", "hourly_cost", "slack_webhook", "alert_escalation_rate", "alert_backlog"}


@router.get("/v1/settings")
def get_settings(p: Principal = Depends(require("read"))):
    with conn() as c:
        s = autonomy.org_settings(c, p.org)
    if p.role != "admin":
        s.pop("slack_webhook", None)
    return jsonable_encoder(s)


@router.put("/v1/settings")
def put_settings(body: dict[str, Any], p: Principal = Depends(require("read"))):
    _admin(p)
    upd = {k: v for k, v in body.items() if k in SETTING_FIELDS}
    if "autonomy_mode" in upd and upd["autonomy_mode"] not in ("shadow", "progressive", "autonomous"):
        raise HTTPException(422, {"error": "bad_mode"})
    with conn() as c:
        autonomy.org_settings(c, p.org)
        if upd:
            sets = ", ".join(f"{k}=%s" for k in upd)
            c.execute(f"UPDATE org_settings SET {sets}, updated_at=now() WHERE org_id=%s", [*upd.values(), p.org])
        if body.get("rotate_widget_token"):
            c.execute("UPDATE org_settings SET widget_token=%s WHERE org_id=%s", ("wt_" + secrets.token_urlsafe(18), p.org))
        audit.log(c, None, p.name, "settings.updated", {"org": p.org, "fields": sorted(upd)})
    autonomy.invalidate(p.org)
    return get_settings(p)


@router.get("/v1/autonomy")
def autonomy_view(p: Principal = Depends(require("read"))):
    with conn() as c:
        s = autonomy.org_settings(c, p.org)
        acts = set(rules.ACTION_FIELDS) | {r["action"] for r in c.execute(
            "SELECT action FROM trust_stats WHERE org_id=%s", (p.org,)).fetchall()}
        items = [autonomy.trust(c, p.org, a, s) for a in sorted(acts)]
    return {"mode": s["autonomy_mode"], "threshold": s["trust_threshold"], "min_samples": s["trust_min_samples"],
            "actions": items}


# ================================================================ policy rules + what-if
class CompileIn(BaseModel):
    text: str = Field(min_length=4, max_length=500)


@router.post("/v1/policies/compile")
def compile_rule(body: CompileIn, p: Principal = Depends(require("read"))):
    rule, method = rules.compile_nl(body.text)
    with conn() as c:
        sim = rules.simulate(c, p.org, [rule], days=30)
    return {"rule": rule, "method": method, "impact": sim}


class RuleIn(BaseModel):
    name: str
    nl_text: str = ""
    rule: dict[str, Any]


@router.get("/v1/policies/rules")
def list_rules(p: Principal = Depends(require("read"))):
    with conn() as c:
        rows = c.execute("SELECT * FROM policy_rules WHERE org_id=%s ORDER BY created_at", (p.org,)).fetchall()
    return {"rules": jsonable_encoder(rows), "actions": rules.ACTION_FIELDS}


@router.post("/v1/policies/rules", status_code=201)
def add_rule(body: RuleIn, p: Principal = Depends(require("read"))):
    _admin(p)
    try:
        r = rules.validate(body.rule)
    except rules.RuleError as e:
        raise HTTPException(422, {"error": "invalid_rule", "detail": str(e)})
    rid = new_id("pr")
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", body.name)[:60] or "rule"
    with conn() as c:
        c.execute("INSERT INTO policy_rules(id, org_id, name, nl_text, rule, created_by) VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                  (rid, p.org, name, body.nl_text, json.dumps(r), p.name))
        audit.log(c, None, p.name, "policy.rule_added", {"id": rid, "org": p.org, "rule": r, "nl": body.nl_text})
    rules.invalidate(p.org)
    return {"id": rid, "rule": r}


@router.patch("/v1/policies/rules/{rule_id}")
def toggle_rule(rule_id: str, body: dict[str, Any], p: Principal = Depends(require("read"))):
    _admin(p)
    with conn() as c:
        n = c.execute("UPDATE policy_rules SET enabled=%s WHERE id=%s AND org_id=%s",
                      (bool(body.get("enabled")), rule_id, p.org)).rowcount
        audit.log(c, None, p.name, "policy.rule_toggled", {"id": rule_id, "enabled": bool(body.get("enabled"))})
    rules.invalidate(p.org)
    if not n:
        raise HTTPException(404, {"error": "not_found"})
    return {"ok": True}


@router.delete("/v1/policies/rules/{rule_id}")
def delete_rule(rule_id: str, p: Principal = Depends(require("read"))):
    _admin(p)
    with conn() as c:
        c.execute("DELETE FROM policy_rules WHERE id=%s AND org_id=%s", (rule_id, p.org))
        audit.log(c, None, p.name, "policy.rule_deleted", {"id": rule_id})
    rules.invalidate(p.org)
    return {"ok": True}


class SimIn(BaseModel):
    rules: list[dict[str, Any]] = []
    days: float = 30
    replace_existing: bool = False


@router.post("/v1/policies/simulate")
def simulate(body: SimIn, p: Principal = Depends(require("read"))):
    try:
        with conn() as c:
            return rules.simulate(c, p.org, body.rules, body.days, body.replace_existing)
    except rules.RuleError as e:
        raise HTTPException(422, {"error": "invalid_rule", "detail": str(e)})


# ================================================================ connectors
class ConnectorIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")
    base_url: str
    spec: dict[str, Any] | None = None
    spec_url: str | None = None
    auth_header: str | None = None
    auth_value: str | None = None
    include: list[str] | None = None


def _abs(url: str) -> str:
    return url if url.startswith("http") else settings.api_base_url.rstrip("/") + "/" + url.lstrip("/")


@router.get("/v1/connectors")
def list_connectors(p: Principal = Depends(require("read"))):
    with conn() as c:
        rows = c.execute("SELECT id, name, base_url, operations, enabled, created_at FROM connectors WHERE org_id=%s ORDER BY name", (p.org,)).fetchall()
    return {"connectors": jsonable_encoder(rows)}


@router.post("/v1/connectors", status_code=201)
def add_connector(body: ConnectorIn, p: Principal = Depends(require("read"))):
    _admin(p)
    spec = body.spec
    if spec is None and body.spec_url:
        try:
            spec = httpx.get(_abs(body.spec_url), timeout=10).json()
        except Exception as e:
            raise HTTPException(422, {"error": "spec_fetch_failed", "detail": str(e)[:200]})
    if not spec:
        raise HTTPException(422, {"error": "spec_required"})
    ops = parse_openapi(spec, body.include)
    if not ops:
        raise HTTPException(422, {"error": "no_operations_found"})
    with conn() as c:
        c.execute(
            """INSERT INTO connectors(id, org_id, name, base_url, auth_header, auth_value, operations)
               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
               ON CONFLICT (org_id, name) DO UPDATE SET base_url=EXCLUDED.base_url, auth_header=EXCLUDED.auth_header,
                   auth_value=EXCLUDED.auth_value, operations=EXCLUDED.operations, enabled=TRUE""",
            (new_id("cn"), p.org, body.name, body.base_url, body.auth_header, body.auth_value, json.dumps(ops)),
        )
        audit.log(c, None, p.name, "connector.registered", {"name": body.name, "operations": sorted(ops)})
    from ..agent import tools as T

    T._conn_cache.pop(p.org, None)
    return {"name": body.name, "operations": {k: {"method": v["method"], "path": v["path"], "summary": v["summary"]} for k, v in ops.items()}}


@router.post("/v1/connectors/demo", status_code=201)
def add_demo_connector(p: Principal = Depends(require("read"))):
    from ..connectors.demo_crm import crm

    return add_connector(ConnectorIn(name="acme_crm", base_url=_abs("/mock/crm"), spec=crm.openapi()), p)


@router.delete("/v1/connectors/{name}")
def delete_connector(name: str, p: Principal = Depends(require("read"))):
    _admin(p)
    with conn() as c:
        c.execute("DELETE FROM connectors WHERE org_id=%s AND name=%s", (p.org, name))
        audit.log(c, None, p.name, "connector.removed", {"name": name})
    from ..agent import tools as T

    T._conn_cache.pop(p.org, None)
    return {"ok": True}


def _call_external(row: dict, op: dict, args: dict, idem: str | None) -> tuple[int, Any]:
    path = op["path"]
    for k in op["path_params"]:
        path = path.replace("{" + k + "}", urllib.parse.quote(str(args.get(k, "")), safe=""))
    query = {k: args[k] for k in op["query_params"] if k in args}
    body = {k: args[k] for k in op["body_params"] if k in args}
    headers = {}
    if row.get("auth_header") and row.get("auth_value"):
        headers[row["auth_header"]] = row["auth_value"]
    if idem:
        headers["Idempotency-Key"] = idem
    try:
        r = httpx.request(op["method"], row["base_url"].rstrip("/") + path, params=query or None,
                          json=body if op["method"] != "GET" else None, headers=headers, timeout=10)
    except httpx.HTTPError as e:
        raise HTTPException(502, {"error": "connector_unreachable", "detail": str(e)[:200]})
    if r.status_code >= 500:
        raise HTTPException(502, {"error": "connector_upstream_error", "status": r.status_code})
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:1000]}
    return r.status_code, data


class InvokeIn(BaseModel):
    operation: str
    args: dict[str, Any] = {}
    approval_id: str | None = None


def _connector(c, org: str, name: str, op_id: str) -> tuple[dict, dict]:
    row = c.execute("SELECT * FROM connectors WHERE org_id=%s AND name=%s AND enabled", (org, name)).fetchone()
    if not row:
        raise HTTPException(404, {"error": "connector_not_found"})
    op = (row["operations"] or {}).get(op_id)
    if not op:
        raise HTTPException(404, {"error": "operation_not_found"})
    return dict(row), op


@router.post("/v1/connectors/{name}/invoke")
def invoke(name: str, body: InvokeIn, request: Request, p: Principal = Depends(require("write"))):
    from .mutation import run_mutation

    wf = request.headers.get("X-Workflow-Id")
    with conn() as c:
        org = org_of_workflow(c, wf, p.org)
        row, op = _connector(c, org, name, body.operation)

    def handler(c, a, ctx):
        status, data = _call_external(row, op, body.args, request.headers.get("Idempotency-Key"))
        if status >= 400:
            raise HTTPException(status, {"error": "connector_rejected", "upstream": data})
        return 200, {"connector": name, "operation": body.operation, "status": status, "result": data}

    return run_mutation(request, p, f"connector.{name}.{body.operation}", body.args, handler, body.approval_id)


@router.post("/v1/connectors/{name}/read")
def read(name: str, body: InvokeIn, request: Request, p: Principal = Depends(require("read"))):
    wf = request.headers.get("X-Workflow-Id")
    with conn() as c:
        org = org_of_workflow(c, wf, p.org)
        row, op = _connector(c, org, name, body.operation)
        dec = policy.decide(c, f"connector.{name}.{body.operation}", body.args, org, wf)
    if op["method"] != "GET" or dec.effect != policy.ALLOW:
        raise HTTPException(403, {"error": "policy_denied", **dec.dict()})
    status, data = _call_external(row, op, body.args, None)
    audit.safe_log(wf, p.name, "connector.read", {"connector": name, "operation": body.operation, "status": status})
    if status >= 400:
        raise HTTPException(status, {"error": "connector_rejected", "upstream": data})
    return {"connector": name, "operation": body.operation, "result": data}


# ================================================================ knowledge
class DocIn(BaseModel):
    title: str
    text: str | None = None
    content_b64: str | None = None
    filename: str | None = None


@router.get("/v1/knowledge/docs")
def kb_docs(p: Principal = Depends(require("read"))):
    with conn() as c:
        rows = c.execute("SELECT * FROM kb_docs WHERE org_id=%s ORDER BY created_at DESC", (p.org,)).fetchall()
    return {"docs": jsonable_encoder(rows)}


@router.post("/v1/knowledge/docs", status_code=201)
def kb_add(body: DocIn, p: Principal = Depends(require("read"))):
    _admin(p)
    text = body.text or ""
    if body.content_b64:
        raw = base64.b64decode(body.content_b64)
        if (body.filename or "").lower().endswith(".pdf"):
            try:
                text = knowledge.pdf_text(raw)
            except ImportError:
                raise HTTPException(422, {"error": "pypdf_not_installed"})
        else:
            text = raw.decode("utf-8", errors="ignore")
    try:
        with conn() as c:
            d = knowledge.ingest(c, p.org, body.title, text, body.filename or "text")
            audit.log(c, None, p.name, "knowledge.ingested", d)
    except ValueError as e:
        raise HTTPException(422, {"error": str(e)})
    return d


@router.delete("/v1/knowledge/docs/{doc_id}")
def kb_delete(doc_id: str, p: Principal = Depends(require("read"))):
    _admin(p)
    with conn() as c:
        c.execute("DELETE FROM kb_docs WHERE id=%s AND org_id=%s", (doc_id, p.org))
    return {"ok": True}


@router.get("/v1/knowledge/search")
def kb_search(request: Request, query: str = Query(..., alias="query"), k: int = 4,
              p: Principal = Depends(require("knowledge:read"))):
    with conn() as c:
        org = org_of_workflow(c, request.headers.get("X-Workflow-Id"), p.org)
        res = knowledge.search(c, org, query, k)
    return {"results": res, "hint": "cite as [kb:<id>]" if res else "no matching knowledge; do not invent policy"}


# ================================================================ replies & feedback
@router.get("/v1/replies")
def list_replies(status: str = "draft", p: Principal = Depends(require("workflow:read"))):
    with conn() as c:
        rows = c.execute(
            """SELECT r.*, t.subject, t.body AS ticket_body, w.status AS workflow_status FROM replies r
               JOIN tickets t ON t.id=r.ticket_id JOIN workflows w ON w.id=r.workflow_id
               WHERE r.org_id=%s AND (%s='all' OR r.status=%s) ORDER BY r.created_at DESC LIMIT 100""",
            (p.org, status, status),
        ).fetchall()
    return {"replies": jsonable_encoder(rows)}


class SendIn(BaseModel):
    text: str | None = None


@router.post("/v1/replies/{reply_id}/send")
def send_reply(reply_id: str, body: SendIn, p: Principal = Depends(require("reply:send"))):
    with conn() as c:
        r = c.execute("SELECT org_id FROM replies WHERE id=%s", (reply_id,)).fetchone()
    if not r or r["org_id"] != p.org:
        raise HTTPException(404, {"error": "not_found"})
    return replies.send(reply_id, p.name, body.text)


@router.post("/v1/replies/{reply_id}/discard")
def discard_reply(reply_id: str, p: Principal = Depends(require("reply:send"))):
    with conn() as c:
        c.execute("UPDATE replies SET status='discarded' WHERE id=%s AND org_id=%s AND status='draft'", (reply_id, p.org))
    return {"ok": True}


class FeedbackIn(BaseModel):
    expected: str = Field(default="any", pattern="^(resolved|escalated|any)$")
    wrong_actions: list[dict[str, Any]] = []
    note: str = ""


@router.post("/v1/workflows/{workflow_id}/feedback", status_code=201)
def feedback(workflow_id: str, body: FeedbackIn, p: Principal = Depends(require("feedback:write"))):
    from ..eval.regression import capture_case

    with conn() as c:
        wf = c.execute("SELECT org_id FROM workflows WHERE id=%s", (workflow_id,)).fetchone()
        if not wf or wf["org_id"] != p.org:
            raise HTTPException(404, {"error": "not_found"})
        cid = capture_case(c, workflow_id, created_by=p.name, reason=body.note or "flagged by operator",
                           forbidden=body.wrong_actions, terminal=body.expected)
    return {"case_id": cid}


@router.get("/v1/eval/cases")
def eval_cases(p: Principal = Depends(require("workflow:read"))):
    with conn() as c:
        rows = c.execute("SELECT id, source_workflow, reason, expect, created_by, created_at, ticket FROM eval_cases WHERE org_id=%s ORDER BY created_at DESC", (p.org,)).fetchall()
    last = Path("results/regression_latest.json")
    return {"cases": jsonable_encoder(rows), "last_run": json.loads(last.read_text()) if last.exists() else None}


@router.delete("/v1/eval/cases/{case_id}")
def delete_case(case_id: str, p: Principal = Depends(require("read"))):
    _admin(p)
    with conn() as c:
        c.execute("DELETE FROM eval_cases WHERE id=%s AND org_id=%s", (case_id, p.org))
    return {"ok": True}


# ================================================================ intake channels
_buckets: dict[str, deque] = defaultdict(deque)


def _rate(key: str, per_min: int = 20) -> None:
    q, now = _buckets[key], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= per_min:
        raise HTTPException(429, {"error": "rate_limited"})
    q.append(now)


def _org_by_token(token: str | None) -> str | None:
    if not token:
        return None
    with conn() as c:
        row = c.execute("SELECT org_id FROM org_settings WHERE widget_token=%s", (token,)).fetchone()
    return row["org_id"] if row else None


def _intake_org(request: Request, token: str | None) -> tuple[str, str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        p = resolve_token(auth[7:].strip())
        if p and p.can("workflow:submit"):
            return p.org, p.name
    org = _org_by_token(token)
    if not org:
        raise HTTPException(401, {"error": "intake_token_required"})
    return org, "intake"


def _customer_for(c, email: str | None, customer_id: str | None, name: str = "") -> str:
    if customer_id:
        return customer_id
    if not email:
        raise HTTPException(422, {"error": "customer_id_or_email_required"})
    row = c.execute("SELECT id FROM customers WHERE lower(email)=lower(%s)", (email,)).fetchone()
    if row:
        return row["id"]
    cid = "GUEST-" + hashlib.sha1(email.lower().encode()).hexdigest()[:8].upper()
    c.execute("INSERT INTO customers(id, name, email) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (cid, name or email, email))
    return cid


class EmailIn(BaseModel):
    model_config = {"populate_by_name": True}
    sender: str = Field(alias="from")
    subject: str = ""
    text: str


@router.post("/v1/intake/email", status_code=202)
def intake_email(body: EmailIn, request: Request, token: str | None = None):
    org, who = _intake_org(request, token)
    _rate("email:" + org, 120)
    m = re.search(r"[^<\s]+@[^>\s]+", body.sender)
    email = m.group(0) if m else body.sender
    with conn() as c:
        cid = _customer_for(c, email, None)
        return create_ticket(c, org_id=org, customer_id=cid, subject=body.subject or "(no subject)", body=body.text,
                             channel="email", reply_to=email, submitted_by=who)


class WebhookIn(BaseModel):
    subject: str
    body: str
    customer_id: str | None = None
    email: str | None = None
    callback_url: str | None = None


@router.post("/v1/intake/webhook", status_code=202)
def intake_webhook(body: WebhookIn, request: Request, token: str | None = None):
    org, who = _intake_org(request, token)
    _rate("hook:" + org, 300)
    with conn() as c:
        cid = _customer_for(c, body.email, body.customer_id)
        return create_ticket(c, org_id=org, customer_id=cid, subject=body.subject, body=body.body,
                             channel="webhook", reply_to=body.callback_url, submitted_by=who)


@router.post("/v1/intake/slack")
async def intake_slack(request: Request, token: str | None = None):
    raw = await request.body()
    if settings.slack_signing_secret:
        ts = request.headers.get("X-Slack-Request-Timestamp", "0")
        base = f"v0:{ts}:{raw.decode()}".encode()
        sig = "v0=" + hmac.new(settings.slack_signing_secret.encode(), base, hashlib.sha256).hexdigest()
        if abs(time.time() - int(ts)) > 300 or not hmac.compare_digest(sig, request.headers.get("X-Slack-Signature", "")):
            raise HTTPException(401, {"error": "bad_slack_signature"})
    form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
    org = _org_by_token(token)
    if not org:
        raise HTTPException(401, {"error": "intake_token_required"})
    text = form.get("text", "").strip()
    if not text:
        return {"response_type": "ephemeral", "text": "Usage: /support <customer id or email> <your request>"}
    first, _, rest = text.partition(" ")
    with conn() as c:
        cid = _customer_for(c, first if "@" in first else None, None if "@" in first else first)
        out = create_ticket(c, org_id=org, customer_id=cid, subject=(rest or text)[:80], body=rest or text,
                            channel="slack", reply_to=form.get("response_url"), submitted_by="slack:" + form.get("user_name", "?"))
    return {"response_type": "ephemeral", "text": f"Ticket {out['ticket_id']} created; ResolveOS is on it."}


class ChatIn(BaseModel):
    token: str
    email: str
    name: str = ""
    message: str = Field(min_length=2, max_length=4000)


@router.post("/v1/intake/chat", status_code=202)
def intake_chat(body: ChatIn, request: Request):
    _rate("chat:" + (request.client.host if request.client else "?"), 10)
    org = _org_by_token(body.token)
    if not org:
        raise HTTPException(401, {"error": "bad_widget_token"})
    from ..auth import sign

    with conn() as c:
        cid = _customer_for(c, body.email, None, body.name)
        out = create_ticket(c, org_id=org, customer_id=cid, subject=body.message[:80], body=body.message,
                            channel="chat", reply_to=body.email, submitted_by="chat-widget")
    return {"ticket_id": out["ticket_id"], "key": sign({"t": out["ticket_id"], "exp": time.time() + 86400 * 7}, "chat")}


@router.get("/v1/intake/chat/{ticket_id}")
def chat_poll(ticket_id: str, k: str):
    d = unsign(k, "chat")
    if not d or d.get("t") != ticket_id:
        raise HTTPException(401, {"error": "bad_chat_key"})
    with conn() as c:
        t = c.execute("SELECT status FROM tickets WHERE id=%s", (ticket_id,)).fetchone()
        rs = c.execute("SELECT final, sent_at FROM replies WHERE ticket_id=%s AND status='sent' ORDER BY sent_at", (ticket_id,)).fetchall()
    return {"status": t["status"] if t else "unknown", "replies": jsonable_encoder(rs)}


@router.get("/widget.js", include_in_schema=False)
def widget_js():
    return FileResponse(UI_DIR / "widget.js", media_type="application/javascript")


@router.get("/widget-demo", include_in_schema=False)
def widget_demo(p: Principal = Depends(require("read"))):
    with conn() as c:
        tok = autonomy.org_settings(c, p.org).get("widget_token") or ""
    return {"snippet": f'<script src="{settings.public_url.rstrip("/")}/widget.js" data-token="{tok}"></script>'}


# ================================================================ one-tap approvals
PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>Approval</title>
<style>body{{font:16px system-ui;background:#05070d;color:#dce6f7;display:grid;place-items:center;min-height:100vh;margin:0}}
.c{{max-width:440px;width:92vw;background:#0d1426;border:1px solid #23314f;border-radius:16px;padding:22px}}
h2{{margin:0 0 6px;font-size:20px}} .m{{color:#7f8ca8;font-size:14px}} code{{color:#ff6fd8}}
table{{width:100%;margin:14px 0;font:14px ui-monospace,monospace}} td{{padding:4px 0}} td:first-child{{color:#7f8ca8}}
button{{width:100%;padding:14px;border-radius:12px;border:0;font:600 16px system-ui;margin-top:10px;cursor:pointer}}
.a{{background:#34e0a1;color:#04150d}} .r{{background:#2a1220;color:#ff4d6d;border:1px solid #ff4d6d}}
input{{width:100%;box-sizing:border-box;padding:11px;border-radius:10px;border:1px solid #23314f;background:#05070d;color:#dce6f7;margin-top:8px}}</style>
<div class=c>{inner}</div>"""


def _check_apv(approval_id: str, t: str) -> None:
    d = unsign(t, "apv")
    if not d or d.get("a") != approval_id:
        raise HTTPException(401, "link expired or invalid")


@router.get("/approve/{approval_id}", response_class=HTMLResponse, include_in_schema=False)
def quick_page(approval_id: str, t: str):
    _check_apv(approval_id, t)
    with conn() as c:
        a = c.execute("SELECT a.*, tk.subject FROM approvals a JOIN workflows w ON w.id=a.workflow_id JOIN tickets tk ON tk.id=w.ticket_id WHERE a.id=%s", (approval_id,)).fetchone()
    if not a:
        raise HTTPException(404)
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    rows = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in (a["args"] or {}).items())
    if a["status"] != "pending":
        inner = f"<h2>Already {esc(a['status'])}</h2><p class=m>{esc(a['action'])} · {esc(a['decided_by'] or '')}</p>"
    else:
        inner = (f"<h2>Approve <code>{esc(a['action'])}</code>?</h2><p class=m>{esc(a['subject'])}</p><table>{rows}</table>"
                 f"<p class=m>{esc(a['justification'] or '')}</p><form method=post><input type=hidden name=t value='{esc(t)}'>"
                 f"<input name=comment placeholder='comment (optional)'>"
                 f"<button class=a name=decision value=approve>Approve</button><button class=r name=decision value=reject>Reject</button></form>")
    return PAGE.format(inner=inner)


@router.post("/approve/{approval_id}", response_class=HTMLResponse, include_in_schema=False)
async def quick_decide(approval_id: str, request: Request):
    form = {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode()).items()}
    _check_apv(approval_id, form.get("t", ""))
    from .app import DecisionIn, decide

    with conn() as c:
        a = c.execute("SELECT org_id FROM approvals WHERE id=%s", (approval_id,)).fetchone()
    if not a:
        raise HTTPException(404)
    try:
        dval = form.get("decision") if form.get("decision") in ("approve", "reject") else "reject"
        r = decide(approval_id, DecisionIn(decision=dval, comment=form.get("comment", "") + " (one-tap)"),
                   Principal("one-tap-approver", "approver", a["org_id"]))
        msg = f"<h2>{'Approved ✓' if r['status'] == 'approved' else 'Rejected'}</h2><p class=m>The workflow is resuming.</p>"
    except HTTPException as e:
        msg = f"<h2>Not applied</h2><p class=m>{e.detail}</p>"
    return PAGE.format(inner=msg)


# ================================================================ ROI, costs, alerts, metrics
@router.get("/v1/roi")
def roi(days: float = 30, p: Principal = Depends(require("workflow:read"))):
    with conn() as c:
        s = autonomy.org_settings(c, p.org)
        w = c.execute(
            """SELECT
                 count(*) FILTER (WHERE w.status IN ('completed','escalated','failed')) AS finished,
                 count(*) FILTER (WHERE w.status='completed' AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.workflow_id=w.id)) AS autonomous,
                 count(*) FILTER (WHERE w.status='completed' AND EXISTS (SELECT 1 FROM approvals a WHERE a.workflow_id=w.id)) AS assisted,
                 count(*) FILTER (WHERE w.status='escalated') AS escalated,
                 percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (w.finished_at-w.created_at))) AS median_ttr_s
               FROM workflows w WHERE w.org_id=%s AND w.created_at > now() - make_interval(days => %s)""",
            (p.org, int(days)),
        ).fetchone()
        cost = c.execute("SELECT COALESCE(sum(cost_usd),0) AS c, COALESCE(sum(input_tokens+output_tokens),0) AS t FROM llm_usage WHERE org_id=%s AND created_at > now() - make_interval(days => %s)", (p.org, int(days))).fetchone()
        protected = c.execute(
            """SELECT COALESCE(sum((a.payload->'args'->>'amount')::numeric),0) AS amt, count(*) AS n FROM audit_log a
               JOIN workflows w ON w.id=a.workflow_id WHERE w.org_id=%s AND a.ts > now() - make_interval(days => %s)
               AND (a.event='gate.denied' OR a.event LIKE '%%.blocked') AND a.payload->'args' ? 'amount'""",
            (p.org, int(days)),
        ).fetchone()
        rejected = c.execute(
            """SELECT COALESCE(sum((args->>'amount')::numeric),0) AS amt, count(*) AS n FROM approvals
               WHERE org_id=%s AND status='rejected' AND created_at > now() - make_interval(days => %s) AND args ? 'amount'""",
            (p.org, int(days)),
        ).fetchone()
    hm, rate = float(s["handle_minutes"]), float(s["hourly_cost"])
    minutes = (w["autonomous"] or 0) * hm + (w["assisted"] or 0) * max(hm - 1.0, 0) + (w["escalated"] or 0) * 2.0
    saved = minutes / 60 * rate
    llm = float(cost["c"])
    return {
        "days": days, "tickets": w["finished"] or 0, "autonomous": w["autonomous"] or 0, "assisted": w["assisted"] or 0,
        "escalated": w["escalated"] or 0,
        "automation_rate": round((w["autonomous"] or 0) / w["finished"], 3) if w["finished"] else 0,
        "median_time_to_resolution_s": round(float(w["median_ttr_s"]), 1) if w["median_ttr_s"] is not None else None,
        "agent_hours_saved": round(minutes / 60, 2), "labor_saved_usd": round(saved, 2),
        "llm_cost_usd": round(llm, 4), "tokens": int(cost["t"]), "net_savings_usd": round(saved - llm, 2),
        "money_protected_usd": round(float(protected["amt"]) + float(rejected["amt"]), 2),
        "risky_actions_stopped": int(protected["n"]) + int(rejected["n"]),
        "assumptions": {"handle_minutes_per_ticket": hm, "hourly_cost_usd": rate,
                        "assisted_ticket_human_minutes": 1.0, "escalated_ticket_minutes_saved": 2.0},
    }


@router.get("/v1/costs")
def costs(days: float = 7, p: Principal = Depends(require("workflow:read"))):
    with conn() as c:
        by_model = c.execute(
            """SELECT provider, model, count(*) AS calls, sum(input_tokens) AS tin, sum(output_tokens) AS tout,
                      sum(cost_usd) AS cost, avg(latency_s) AS lat, count(*) FILTER (WHERE NOT ok) AS errors
               FROM llm_usage WHERE org_id=%s AND created_at > now() - make_interval(days => %s)
               GROUP BY provider, model ORDER BY calls DESC""", (p.org, int(days))).fetchall()
        reasons = c.execute(
            """SELECT route_reason, count(*) AS n FROM llm_usage WHERE org_id=%s AND purpose='agent_step'
               AND created_at > now() - make_interval(days => %s) GROUP BY route_reason ORDER BY n DESC LIMIT 10""",
            (p.org, int(days))).fetchall()
        per_ticket = c.execute(
            """SELECT avg(s) AS avg FROM (SELECT sum(cost_usd) AS s FROM llm_usage WHERE org_id=%s AND workflow_id IS NOT NULL
               AND created_at > now() - make_interval(days => %s) GROUP BY workflow_id) x""", (p.org, int(days))).fetchone()
    from ..agent.llm import providers

    return jsonable_encoder({"by_model": by_model, "route_reasons": reasons,
                             "avg_cost_per_ticket": float(per_ticket["avg"] or 0), "router_mode": settings.router_mode,
                             "providers": {k: {"model": v.model, "price_in": v.price_in, "price_out": v.price_out}
                                           for k, v in providers().items()}})


@router.get("/v1/alerts")
def alerts(p: Principal = Depends(require("workflow:read"))):
    with conn() as c:
        rows = c.execute("SELECT * FROM alerts WHERE org_id=%s AND resolved_at IS NULL ORDER BY created_at DESC", (p.org,)).fetchall()
    return {"alerts": jsonable_encoder(rows)}


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def metrics(token: str | None = None):
    if settings.metrics_token and token != settings.metrics_token:
        raise HTTPException(401)
    L = []
    with conn() as c:
        for r in c.execute("SELECT org_id, status, count(*) AS n FROM workflows GROUP BY org_id, status").fetchall():
            L.append(f'resolveos_workflows{{org="{r["org_id"]}",status="{r["status"]}"}} {r["n"]}')
        for r in c.execute("SELECT org_id, count(*) AS n FROM approvals WHERE status='pending' GROUP BY org_id").fetchall():
            L.append(f'resolveos_pending_approvals{{org="{r["org_id"]}"}} {r["n"]}')
        for r in c.execute("SELECT provider, model, count(*) AS n, sum(cost_usd) AS c, sum(input_tokens+output_tokens) AS t, count(*) FILTER (WHERE NOT ok) AS e FROM llm_usage GROUP BY provider, model").fetchall():
            lbl = f'provider="{r["provider"]}",model="{r["model"]}"'
            L += [f"resolveos_llm_calls_total{{{lbl}}} {r['n']}", f"resolveos_llm_cost_usd_total{{{lbl}}} {float(r['c'])}",
                  f"resolveos_llm_tokens_total{{{lbl}}} {r['t']}", f"resolveos_llm_errors_total{{{lbl}}} {r['e']}"]
        lat = c.execute("""SELECT percentile_cont(ARRAY[0.5,0.95]) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (finished_at-created_at))) AS q
                           FROM (SELECT * FROM workflows WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 500) x""").fetchone()
        if lat and lat["q"]:
            L += [f'resolveos_workflow_seconds{{quantile="0.5"}} {lat["q"][0]}', f'resolveos_workflow_seconds{{quantile="0.95"}} {lat["q"][1]}']
        for r in c.execute("SELECT event, count(*) AS n FROM audit_log WHERE event LIKE '%%.replayed' OR event LIKE 'guardrail.%%' OR event='gate.denied' GROUP BY event").fetchall():
            L.append(f'resolveos_events_total{{event="{r["event"]}"}} {r["n"]}')
        L.append(f"resolveos_alerts_open {c.execute('SELECT count(*) AS n FROM alerts WHERE resolved_at IS NULL').fetchone()['n']}")
    return "\n".join(L) + "\n"
