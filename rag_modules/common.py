from __future__ import annotations

from datetime import datetime, timezone
import re


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def safe_collection_name(value: str) -> str:
    name = re.sub(r"[^0-9A-Za-z_]", "_", value.strip())
    if not name or not re.match(r"^[A-Za-z_]", name):
        name = f"kb_{name}"
    return name[:255]
