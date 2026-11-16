"""Shadow mode and progressive autonomy.

Modes (per org):
  shadow      - the agent may only PROPOSE side effects; every write needs a human.
  progressive - writes that policy would allow run automatically only once that action type
                has earned trust: >= trust_min_samples human reviews with an approval rate
                >= trust_threshold. A rejection lowers the rate and can demote it again.
  autonomous  - policy alone decides (default; used by the benchmark).
"""
from __future__ import annotations

import time

_settings_cache: dict[str, tuple[float, dict]] = {}


def org_settings(c, org_id: str) -> dict:
    hit = _settings_cache.get(org_id)
    if hit and time.time() - hit[0] < 2.0:
        return hit[1]
    row = c.execute("SELECT * FROM org_settings WHERE org_id=%s", (org_id,)).fetchone()
    if not row:
        c.execute("INSERT INTO org_settings(org_id) VALUES (%s) ON CONFLICT DO NOTHING", (org_id,))
        row = c.execute("SELECT * FROM org_settings WHERE org_id=%s", (org_id,)).fetchone()
    _settings_cache[org_id] = (time.time(), dict(row))
    return dict(row)


def invalidate(org_id: str) -> None:
    _settings_cache.pop(org_id, None)


def trust(c, org_id: str, action: str, s: dict | None = None) -> dict:
    s = s or org_settings(c, org_id)
    row = c.execute("SELECT * FROM trust_stats WHERE org_id=%s AND action=%s", (org_id, action)).fetchone()
    n, ok = (row["proposals"], row["approved"]) if row else (0, 0)
    rate = ok / n if n else 0.0
    return {"action": action, "reviews": n, "approved": ok, "rejected": (row["rejected"] if row else 0),
            "approval_rate": round(rate, 3), "needed": max(s["trust_min_samples"] - n, 0),
            "trusted": n >= s["trust_min_samples"] and rate >= s["trust_threshold"]}


def record_review(c, org_id: str, action: str, approved: bool) -> None:
    c.execute(
        """INSERT INTO trust_stats(org_id, action, proposals, approved, rejected) VALUES (%s,%s,1,%s,%s)
           ON CONFLICT (org_id, action) DO UPDATE SET proposals = trust_stats.proposals + 1,
               approved = trust_stats.approved + EXCLUDED.approved, rejected = trust_stats.rejected + EXCLUDED.rejected,
               updated_at = now()""",
        (org_id, action, 1 if approved else 0, 0 if approved else 1),
    )
