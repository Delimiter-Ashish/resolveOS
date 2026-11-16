"""Knowledge base: ingest documents (text, markdown, PDF), chunk them, and retrieve with
PostgreSQL full-text search (ranked, no extra infrastructure). Results carry citation ids."""
from __future__ import annotations

import io
import re

from .tickets import new_id

STOP = set("a an the and or of to in on for is are was were be been with my your our it this that i you we me "
           "please can could would should do does did have has had not no yes at by from as".split())


def chunk(text: str, size: int = 900, overlap: int = 120) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 2 <= size:
            cur = (cur + "\n\n" + p).strip()
            continue
        if cur:
            chunks.append(cur)
        while len(p) > size:
            chunks.append(p[:size])
            p = p[size - overlap:]
        cur = p
    if cur:
        chunks.append(cur)
    return chunks


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    return "\n\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(data)).pages)


def ingest(c, org_id: str, title: str, text: str, source: str = "upload") -> dict:
    parts = chunk(text)
    if not parts:
        raise ValueError("document is empty")
    did = new_id("kb")
    c.execute("INSERT INTO kb_docs(id, org_id, title, source, chunks) VALUES (%s,%s,%s,%s,%s)",
              (did, org_id, title[:200], source, len(parts)))
    for i, p in enumerate(parts):
        c.execute("INSERT INTO kb_chunks(doc_id, org_id, ord, text) VALUES (%s,%s,%s,%s)", (did, org_id, i, p))
    return {"id": did, "title": title, "chunks": len(parts)}


def search(c, org_id: str, query: str, k: int = 4) -> list[dict]:
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", query.lower()) if w not in STOP and len(w) > 1][:24]
    if not words:
        return []
    tsq = " | ".join(words)
    rows = c.execute(
        """SELECT ch.id, ch.text, d.title, ts_rank_cd(ch.tsv, to_tsquery('english', %s)) AS score
           FROM kb_chunks ch JOIN kb_docs d ON d.id = ch.doc_id
           WHERE ch.org_id=%s AND ch.tsv @@ to_tsquery('english', %s)
           ORDER BY score DESC LIMIT %s""",
        (tsq, org_id, tsq, k),
    ).fetchall()
    return [{"id": r["id"], "title": r["title"], "score": round(float(r["score"]), 4), "text": r["text"][:700]}
            for r in rows]
