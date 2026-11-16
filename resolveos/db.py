"""PostgreSQL access: pooled connections, schema init, key seeding, reset.

CLI:
    python -m resolveos.db init     # create schema + API keys (+ LangGraph checkpoint tables)
    python -m resolveos.db reset    # truncate all data (keeps API keys)
"""
from __future__ import annotations

import atexit
import hashlib
import secrets
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_lock = threading.Lock()
_pool: ConnectionPool | None = None
_ckpt_pool: ConnectionPool | None = None


def _close_pools() -> None:
    for p in (_pool, _ckpt_pool):
        try:
            if p is not None:
                p.close(timeout=1)
        except Exception:
            pass


atexit.register(_close_pools)


def pool() -> ConnectionPool:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ConnectionPool(
                settings.database_url,
                min_size=2,
                max_size=settings.db_pool_size,
                kwargs={"row_factory": dict_row},
                open=True,
                timeout=30,
            )
        return _pool


def checkpoint_pool() -> ConnectionPool:
    """Separate autocommit pool required by the LangGraph Postgres checkpointer."""
    global _ckpt_pool
    with _lock:
        if _ckpt_pool is None:
            _ckpt_pool = ConnectionPool(
                settings.database_url,
                min_size=2,
                max_size=max(8, settings.worker_threads * 2),
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                open=True,
                timeout=30,
            )
        return _ckpt_pool


@contextmanager
def conn():
    """Transactional connection: commits on clean exit, rolls back on exception."""
    with pool().connection() as c:
        yield c


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
KEY_FILE = Path(".env.keys")

ROLES = {
    "agent": ("resolveos-agent", "agent_api_key"),
    "approver": ("resolveos-approver", "approver_api_key"),
    "operator": ("resolveos-operator", "operator_api_key"),
    "admin": ("resolveos-admin", "admin_api_key"),
}
EXTRA_SECRETS = ["secret_key"]


def init_schema() -> None:
    with conn() as c:
        c.execute(SCHEMA_PATH.read_text())


def seed_api_keys() -> dict[str, str]:
    """Create keys for each role (stored hashed). Plaintext keys go to .env.keys
    the first time; if a key is already configured it is reused."""
    keys: dict[str, str] = {}
    lines = []
    for role, (principal, attr) in ROLES.items():
        key = getattr(settings, attr) or f"rk_{role}_{secrets.token_urlsafe(24)}"
        keys[role] = key
        lines.append(f"RESOLVEOS_{attr.upper()}={key}")
        with conn() as c:
            c.execute(
                """INSERT INTO api_keys(principal, role, key_hash) VALUES (%s,%s,%s)
                   ON CONFLICT (principal) DO UPDATE SET key_hash = EXCLUDED.key_hash, active = TRUE""",
                (principal, role, hash_key(key)),
            )
    for attr in EXTRA_SECRETS:
        val = getattr(settings, attr) or secrets.token_urlsafe(32)
        setattr(settings, attr, val)
        lines.append(f"RESOLVEOS_{attr.upper()}={val}")
    pw = None
    with conn() as c:
        if not c.execute("SELECT 1 AS x FROM users WHERE email='admin@resolveos.local'").fetchone():
            from .auth import hash_password
            pw = secrets.token_urlsafe(10)
            c.execute("INSERT INTO users(org_id,email,name,role,pw_hash) VALUES ('default','admin@resolveos.local','Admin','admin',%s)",
                      (hash_password(pw),))
    if pw:
        lines.append(f"# UI login: admin@resolveos.local / {pw}")
        print(f"UI login created: admin@resolveos.local / {pw}")
    if not all(getattr(settings, a) for _, a in ROLES.values()) or pw or not KEY_FILE.exists():
        if KEY_FILE.exists():  # keep previously written lines (e.g. the login comment)
            old = [l for l in KEY_FILE.read_text().splitlines() if l.startswith("# UI login")]
            lines = [l for l in lines if not l.startswith("# UI login")] + (old if not pw else []) + ([f"# UI login: admin@resolveos.local / {pw}"] if pw else [])
        KEY_FILE.write_text("\n".join(lines) + "\n")
        KEY_FILE.chmod(0o600)
        print(f"wrote API keys to {KEY_FILE.resolve()}")
    return keys


def init_checkpointer() -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    PostgresSaver(checkpoint_pool()).setup()


BUSINESS_TABLES = ["refunds", "credits", "reships", "purchase_orders", "shipments", "orders",
                   "accounts", "inventory", "customers"]
DATA_TABLES = ["audit_log", "approvals", "workflows", "tickets", "replies", "outbox", "llm_usage",
               "alerts", "trust_stats", "idempotency_keys", "chaos_hits"] + BUSINESS_TABLES
# kept across resets (configuration): orgs, users, api_keys, org_settings, policy_rules,
# connectors, kb_docs/kb_chunks, eval_cases
CHECKPOINT_TABLES = ["checkpoint_writes", "checkpoint_blobs", "checkpoints"]


def reset_business() -> None:
    with conn() as c:
        c.execute("TRUNCATE " + ", ".join(BUSINESS_TABLES) + " CASCADE")


def reset_data() -> None:
    with conn() as c:
        c.execute("TRUNCATE " + ", ".join(DATA_TABLES) + " RESTART IDENTITY CASCADE")
        for t in CHECKPOINT_TABLES:
            try:
                c.execute(f"TRUNCATE {t}")
            except Exception:  # table not created yet
                c.rollback()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "init"
    if cmd == "init":
        init_schema()
        seed_api_keys()
        init_checkpointer()
        print("database initialised")
    elif cmd == "reset":
        reset_data()
        print("data reset")
    else:
        raise SystemExit("usage: python -m resolveos.db [init|reset]")


if __name__ == "__main__":
    main()
