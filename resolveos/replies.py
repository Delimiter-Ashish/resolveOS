"""Customer reply drafting + delivery.

After a workflow finishes, a reply is drafted in the org's brand tone from what actually happened
(committed actions, policy denials, approvals) plus knowledge-base citations. Depending on org
settings it is sent automatically or waits for a human in the Inbox. Delivery adapters: email
(SMTP), Slack (response_url), web chat widget (polled), webhook callback, or the outbox log.
"""
from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage

import httpx

from . import audit, knowledge
from .autonomy import org_settings
from .config import settings
from .db import conn
from .tickets import new_id

PROMPT = """You write the reply to a customer on behalf of a support team.
Tone: {tone}. Keep it under 120 words. Be specific about what was done. Never promise anything that was
not actually done. If something was declined, explain why kindly and say what the customer can do next.
Do not mention internal tools, policies by rule id, or that you are an AI unless asked.
If knowledge-base excerpts are relevant, use them and keep their [kb:id] markers.

Customer ticket:
Subject: {subject}
{body}

What actually happened (system log):
{facts}

Outcome: {outcome}. Internal summary: {summary}

Knowledge-base excerpts:
{kb}

Write only the reply body."""


def _facts(c, workflow_id: str) -> list[str]:
    out = []
    for r in c.execute(
        """SELECT event, payload FROM audit_log WHERE workflow_id=%s AND
           (event LIKE '%%.committed' OR event IN ('gate.denied','approval.decided')) ORDER BY id""",
        (workflow_id,),
    ).fetchall():
        p = r["payload"]
        if r["event"] == "gate.denied":
            out.append(f"DECLINED by policy: {p.get('tool')} ({p.get('reason')})")
        elif r["event"] == "approval.decided":
            out.append(f"human {p.get('decision')} an action")
        elif not r["event"].startswith("ticket."):
            out.append(f"DONE: {r['event'].removesuffix('.committed')} {json.dumps(p.get('args'))}")
    return out or ["no actions were taken"]


def _template(t: dict, outcome: str, facts: list[str]) -> str:
    done = [f.removeprefix("DONE: ") for f in facts if f.startswith("DONE")]
    declined = [f for f in facts if f.startswith("DECLINED")]
    lines = [f"Hi,", "", f"Thanks for reaching out about \"{t['subject']}\"."]
    if done:
        lines.append("We've taken care of it: " + "; ".join(d.split(" {")[0].replace(".", " ") for d in done) + ".")
    if declined:
        lines.append("Unfortunately part of your request couldn't be completed under our policy.")
    if outcome == "escalated":
        lines.append("A member of our team is reviewing your request and will follow up shortly.")
    lines += ["", "Best regards,", "Support team"]
    return "\n".join(lines)


def draft(workflow_id: str) -> str | None:
    if not settings.reply_drafts:
        return None
    with conn() as c:
        wf = c.execute("SELECT * FROM workflows WHERE id=%s", (workflow_id,)).fetchone()
        t = c.execute("SELECT * FROM tickets WHERE id=%s", (wf["ticket_id"],)).fetchone()
        s = org_settings(c, wf["org_id"])
        facts = _facts(c, workflow_id)
        kb = knowledge.search(c, wf["org_id"], t["subject"] + " " + t["body"], k=3)
    result = wf["result"] or {}
    outcome = {"completed": "resolved", "escalated": "escalated to a human"}.get(wf["status"], wf["status"])
    model = "template"
    try:
        from .agent.llm import complete_text

        text, meta = complete_text(PROMPT.format(
            tone=s["brand_tone"], subject=t["subject"], body=t["body"], facts="\n".join(facts), outcome=outcome,
            summary=result.get("summary", ""), kb="\n".join(f"[kb:{k['id']}] {k['text'][:400]}" for k in kb) or "none",
        ), purpose="reply_draft", workflow_id=workflow_id, org_id=wf["org_id"])
        model = meta["model"]
    except Exception:
        text = _template(dict(t), wf["status"], facts)
    rid = new_id("rp")
    auto = not s["reply_approval"] and s["autonomy_mode"] == "autonomous" and wf["status"] == "completed" \
        and not (wf["flags"] or {}).get("injection")
    with conn() as c:
        c.execute(
            """INSERT INTO replies(id, org_id, workflow_id, ticket_id, channel, to_addr, draft, citations, model)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
            (rid, wf["org_id"], workflow_id, t["id"], t["channel"], t["reply_to"], text.strip(),
             json.dumps([k["id"] for k in kb]), model),
        )
        audit.log(c, workflow_id, "replies", "reply.drafted", {"reply_id": rid, "model": model, "auto_send": auto})
    if auto:
        send(rid, by="auto")
    return rid


def _smtp(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
        s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


def send(reply_id: str, by: str, final_text: str | None = None) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM replies WHERE id=%s FOR UPDATE", (reply_id,)).fetchone()
        if not r or r["status"] != "draft":
            return {"ok": False, "error": "not_a_draft"}
        t = c.execute("SELECT subject FROM tickets WHERE id=%s", (r["ticket_id"],)).fetchone()
        body = (final_text or r["draft"]).strip()
        status, err = "delivered", None
        try:
            if r["channel"] == "email" and r["to_addr"] and settings.smtp_host:
                _smtp(r["to_addr"], "Re: " + t["subject"], body)
            elif r["channel"] in ("slack", "webhook") and (r["to_addr"] or "").startswith("http"):
                httpx.post(r["to_addr"], json={"text": body, "ticket_id": r["ticket_id"]}, timeout=8)
            elif r["channel"] == "chat":
                status = "delivered"  # the widget polls replies
            else:
                status = "logged"     # no transport configured: kept in the outbox
        except Exception as e:
            status, err = "failed", str(e)[:300]
        c.execute("INSERT INTO outbox(org_id, channel, to_addr, subject, body, ticket_id, status, error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                  (r["org_id"], r["channel"], r["to_addr"], "Re: " + t["subject"], body, r["ticket_id"], status, err))
        c.execute("UPDATE replies SET status='sent', final=%s, sent_at=now(), sent_by=%s WHERE id=%s", (body, by, reply_id))
        audit.log(c, r["workflow_id"], by, "reply.sent", {"reply_id": reply_id, "channel": r["channel"], "delivery": status})
    return {"ok": True, "delivery": status, "error": err}
