from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_iso8601(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_tag(tag: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", tag.strip().lower())
    normalized = normalized.strip("_")
    return normalized or "capture"


@dataclass(frozen=True)
class SessionPaths:
    session_id: str
    session_dir: Path
    manifest_path: Path


def create_session_paths(
    tag: str,
    *,
    root_dir: Path | None = None,
    now: datetime | None = None,
) -> SessionPaths:
    current_time = now or utc_now()
    session_id = current_time.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_dir = (root_dir or (Path.cwd() / "output-files" / "captures")) / f"{session_id}_{sanitize_tag(tag)}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return SessionPaths(
        session_id=session_id,
        session_dir=session_dir,
        manifest_path=session_dir / "capture_manifest.json",
    )


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
