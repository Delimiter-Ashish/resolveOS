"""Pretty-print a workflow's audit trail:  python -m resolveos.trace <workflow_id>"""
import sys

from . import audit
from .db import conn


def main() -> None:
    wid = sys.argv[1]
    with conn() as c:
        rows = c.execute("SELECT ts_iso, actor, event, payload FROM audit_log WHERE workflow_id=%s ORDER BY id", (wid,)).fetchall()
    for r in rows:
        p = str(r["payload"])
        print(f"{r['ts_iso'][11:23]}  {r['actor']:<22} {r['event']:<34} {p[:110]}")
    print("chain:", audit.verify_chain(wid))


if __name__ == "__main__":
    main()
