"""Outbound notifications: Slack (incoming webhook) with one-tap approval links, and alerts.
One-tap links carry a signed, expiring token; opening the link shows a confirmation page and the
decision is only taken on an explicit POST (link previewers cannot approve anything)."""
from __future__ import annotations

import time

import httpx

from .config import settings


def approval_token(approval_id: str, ttl_s: float | None = None) -> str:
    from .auth import sign

    return sign({"a": approval_id, "exp": time.time() + (ttl_s or settings.approval_ttl_s)}, "apv")


def approval_link(approval_id: str) -> str:
    return f"{settings.public_url.rstrip('/')}/approve/{approval_id}?t={approval_token(approval_id)}"


def slack(org_id: str, text: str, blocks: list | None = None) -> bool:
    from .autonomy import org_settings
    from .db import conn

    with conn() as c:
        url = org_settings(c, org_id).get("slack_webhook")
    if not url:
        return False
    try:
        httpx.post(url, json={"text": text, **({"blocks": blocks} if blocks else {})}, timeout=5)
        return True
    except Exception:
        return False


def approval_requested(ap: dict) -> None:
    try:
        args = ", ".join(f"{k}={v}" for k, v in (ap.get("args") or {}).items())
        link = approval_link(ap["id"])
        text = f":warning: Approval needed: *{ap['action']}* ({args})\n{ap.get('justification','')}\n<{link}|Review & decide>"
        slack(ap.get("org_id", "default"), text)
    except Exception:
        pass
