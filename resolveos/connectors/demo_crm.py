"""A small mock CRM with its own OpenAPI spec, mounted at /mock/crm, to demonstrate that ANY
REST API can be plugged in as agent tools with one registration call."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..db import conn

crm = FastAPI(title="Acme CRM (demo)", version="1.0",
              description="Mock CRM: customer profiles, notes, loyalty tiers and callback tasks.")
# state lives in Postgres so every API process sees the same CRM
_DDL = "CREATE TABLE IF NOT EXISTS mock_crm (k TEXT PRIMARY KEY, v JSONB NOT NULL)"


def _get(k: str, default):
    with conn() as c:
        c.execute(_DDL)
        r = c.execute("SELECT v FROM mock_crm WHERE k=%s", (k,)).fetchone()
    return r["v"] if r else default


def _put(k: str, v) -> None:
    with conn() as c:
        c.execute(_DDL)
        c.execute("INSERT INTO mock_crm(k, v) VALUES (%s,%s::jsonb) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (k, json.dumps(v)))


def _profile(cid: str) -> dict:
    return _get("p:" + cid, {"customer_id": cid, "loyalty_tier": "silver", "lifetime_value": 420.0,
                             "nps": 8, "preferred_channel": "email"})


@crm.get("/customers/{customer_id}", operation_id="get_crm_profile", summary="Get a customer's CRM profile: loyalty tier, lifetime value, NPS")
def get_profile(customer_id: str):
    return {**_profile(customer_id), "notes": _get("n:" + customer_id, [])[-5:]}


class NoteIn(BaseModel):
    text: str = Field(description="Note to attach to the customer record")


@crm.post("/customers/{customer_id}/notes", operation_id="add_crm_note", summary="Attach an internal note to a customer's CRM record")
def add_note(customer_id: str, body: NoteIn):
    n = {"text": body.text, "at": datetime.now(timezone.utc).isoformat()}
    _put("n:" + customer_id, _get("n:" + customer_id, []) + [n])
    return {"ok": True, "note": n}


class TierIn(BaseModel):
    tier: str = Field(description="New loyalty tier: bronze, silver, gold or platinum")


@crm.put("/customers/{customer_id}/tier", operation_id="set_loyalty_tier", summary="Change a customer's loyalty tier")
def set_tier(customer_id: str, body: TierIn):
    if body.tier not in ("bronze", "silver", "gold", "platinum"):
        raise HTTPException(422, "invalid tier")
    p = _profile(customer_id)
    p["loyalty_tier"] = body.tier
    _put("p:" + customer_id, p)
    return p


class TaskIn(BaseModel):
    customer_id: str
    reason: str = Field(description="Why a human should call the customer back")
    priority: str = Field(default="normal", description="low, normal or high")


@crm.post("/tasks", operation_id="create_callback_task", summary="Create a callback task for the human support team")
def create_task(body: TaskIn):
    tasks = _get("tasks", [])
    t = {"id": f"task_{len(tasks)+1}", **body.model_dump(), "status": "open"}
    _put("tasks", tasks + [t])
    return t
