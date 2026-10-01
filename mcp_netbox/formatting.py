"""Response formatting helpers (JSON and Markdown)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence


def to_json(data: Any) -> str:
    """Serialize ``data`` to pretty-printed JSON."""
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def _display(value: Any) -> str:
    """Render a NetBox field value as a compact string.

    NetBox represents nested objects as ``{"id":..,"url":..,"display":..}``
    and choices as ``{"value":..,"label":..}``.
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        if "display" in value:
            return str(value["display"])
        if "label" in value:
            return str(value["label"])
        if "name" in value:
            return str(value["name"])
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (list, tuple)):
        return ", ".join(_display(v) for v in value)
    return str(value)


def _pick(obj: Dict[str, Any], *keys: str) -> str:
    """Return the first non-empty value among ``keys``."""
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return _display(obj[key])
    return ""


def format_list_markdown(data: Dict[str, Any], columns: Sequence[str],
                         title: str, offset: int) -> str:
    """Format a paginated list response as a Markdown table.

    ``columns`` is a sequence of field names (or dotted paths) to show.
    """
    results: List[Dict[str, Any]] = data.get("results", [])
    count: int = data.get("count", len(results))

    lines: List[str] = []
    lines.append(f"## {title}")
    lines.append("")
    lines.append(f"**Total:** {count} | **Showing:** {len(results)} "
                 f"(offset {offset})")
    lines.append("")

    if not results:
        lines.append("_No results matched the given filters._")
        return "\n".join(lines)

    # Header
    header = "| " + " | ".join(columns) + " |"
    separator = "|" + "|".join([" --- "] * len(columns)) + "|"
    lines.append(header)
    lines.append(separator)

    for item in results:
        row = []
        for col in columns:
            if "." in col:
                parts = col.split(".")
                val: Any = item
                for p in parts:
                    if isinstance(val, dict):
                        val = val.get(p)
                    else:
                        val = None
                        break
                row.append(_display(val))
            else:
                row.append(_display(item.get(col)))
        lines.append("| " + " | ".join(row) + " |")

    if data.get("next"):
        lines.append("")
        lines.append(f"_More results available — increase `offset` to "
                     f"{offset + len(results)} to page forward._")

    return "\n".join(lines)


def format_object_markdown(obj: Dict[str, Any], title: str,
                           fields: Optional[Sequence[str]] = None) -> str:
    """Format a single object as a Markdown key/value list.

    If ``fields`` is None, all scalar-ish fields are shown.
    """
    lines: List[str] = []
    name = _pick(obj, "display", "name", "prefix", "address", "title")
    lines.append(f"## {title}: {name or obj.get('id', '')}")
    lines.append("")

    if fields is None:
        # Show a curated set of common fields, then any other scalar fields.
        preferred = ["id", "display", "name", "slug", "status", "description",
                     "tenant", "site", "region", "location", "rack",
                     "device", "role", "manufacturer", "model", "serial",
                     "asset_tag", "platform", "primary_ip", "primary_ip4",
                     "primary_ip6", "prefix", "vlan", "vrf", "vid", "name",
                     "provider", "cid", "type", "cluster", "zone", "record",
                     "value", "type", "ttl", "is_active", "created",
                     "last_updated"]
        seen = set()
        keys: List[str] = []
        for k in preferred:
            if k in obj and k not in seen:
                keys.append(k)
                seen.add(k)
        for k in obj:
            if k not in seen and k not in {"url", "custom_fields", "tags",
                                           "display", "secrets"}:
                v = obj[k]
                if isinstance(v, (str, int, float, bool)) or v is None:
                    keys.append(k)
                    seen.add(k)
        fields = keys

    for field in fields:
        value = obj.get(field)
        if value in (None, "", [], {}):
            continue
        lines.append(f"- **{field}:** {_display(value)}")

    return "\n".join(lines)


def format_list_json(data: Dict[str, Any], offset: int) -> str:
    """Format a paginated list response as JSON with pagination metadata."""
    results = data.get("results", [])
    count = data.get("count", len(results))
    payload = {
        "total": count,
        "count": len(results),
        "offset": offset,
        "has_more": data.get("next") is not None,
        "next_offset": (offset + len(results)) if data.get("next") else None,
        "results": results,
    }
    return to_json(payload)


def format_object_json(obj: Dict[str, Any]) -> str:
    """Format a single object as JSON (URLs stripped for brevity)."""
    slim = {k: v for k, v in obj.items() if k != "url"}
    return to_json(slim)