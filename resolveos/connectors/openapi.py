"""Turn an OpenAPI 3 spec into agent tools: one tool per operation, typed parameters,
read/write classification by HTTP method, and a policy action per operation."""
from __future__ import annotations

import re
from typing import Any

METHODS = ("get", "post", "put", "patch", "delete")


def _resolve(spec: dict, node: Any, depth: int = 0) -> Any:
    """Inline local $refs (depth counts reference hops, guarding against cycles)."""
    if depth > 8:
        return {}
    if isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return {}
        cur: Any = spec
        for part in ref[2:].split("/"):
            cur = cur.get(part, {}) if isinstance(cur, dict) else {}
        return _resolve(spec, cur, depth + 1)
    if isinstance(node, dict):
        return {k: _resolve(spec, v, depth) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(spec, v, depth) for v in node]
    return node


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", s).strip("_")
    return s[:48] or "op"


def _clean_schema(sch: dict) -> dict:
    keep = {"type", "description", "enum", "items", "format", "minimum", "maximum", "default"}
    out = {k: v for k, v in (sch or {}).items() if k in keep}
    if "anyOf" in (sch or {}):  # pydantic Optional[...] style
        types = [x for x in sch["anyOf"] if x.get("type") != "null"]
        if types:
            out.update({k: v for k, v in types[0].items() if k in keep})
    if "type" not in out:
        out["type"] = "string"
    if out.get("type") == "array" and isinstance(out.get("items"), dict):
        out["items"] = _clean_schema(out["items"])
    return out


def parse(spec: dict, include: list[str] | None = None) -> dict[str, dict]:
    """Returns {op_id: {method, path, summary, path_params, query_params, body_params, schema}}."""
    ops: dict[str, dict] = {}
    for path, item in (spec.get("paths") or {}).items():
        item = _resolve(spec, item)
        shared = item.get("parameters", [])
        for m in METHODS:
            op = item.get(m)
            if not op:
                continue
            op_id = _slug(op.get("operationId") or f"{m}_{path}")
            if include and op_id not in include:
                continue
            props: dict[str, Any] = {}
            required: list[str] = []
            path_p, query_p, body_p = [], [], []
            for prm in shared + op.get("parameters", []):
                prm = _resolve(spec, prm)
                where, name = prm.get("in"), prm.get("name")
                if where not in ("path", "query") or not name:
                    continue
                props[name] = {**_clean_schema(prm.get("schema", {})), **({"description": prm["description"]} if prm.get("description") else {})}
                (path_p if where == "path" else query_p).append(name)
                if prm.get("required") or where == "path":
                    required.append(name)
            body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
            if body:
                body = _resolve(spec, body)
                for name, sch in (body.get("properties") or {}).items():
                    props[name] = _clean_schema(sch)
                    body_p.append(name)
                required += [r for r in body.get("required", []) if r in props]
            desc = (op.get("summary") or op.get("description") or f"{m.upper()} {path}").strip()[:300]
            ops[op_id] = {
                "method": m.upper(), "path": path, "summary": desc,
                "path_params": path_p, "query_params": query_p, "body_params": body_p,
                "schema": {"type": "object", "properties": props, "required": sorted(set(required))},
            }
    return ops
