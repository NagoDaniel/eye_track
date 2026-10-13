"""`raw/meta.json`: the session row plus the study-config snapshot (PLAN §7; DECISIONS 1b item 7).

Written at session creation and rewritten at `/complete` with the final row. The `config` part is
the snapshot taken at creation and is never changed afterwards.
"""

import json
from pathlib import Path
from typing import Any

from app.models import Session as SessionRow
from app.storage import atomic_write_bytes, meta_path


def _row_fields(s: SessionRow) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "study_id": s.study_id,
        "participant_code": s.participant_code,
        "created_at": s.created_at.isoformat(),
        "status": s.status.value,
        "consent_version": s.consent_version,
        "consent_at": s.consent_at.isoformat() if s.consent_at else None,
        "is_debug": s.is_debug,
        "flags": list(s.flags),
        "client_info": s.client_info,
    }


def write_meta(root: Path, s: SessionRow, config: dict[str, Any]) -> None:
    doc = {**_row_fields(s), "config": config}
    data = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")
    atomic_write_bytes(meta_path(root, s.study_id, s.id), data)


def read_config(root: Path, s: SessionRow) -> dict[str, Any]:
    """The config snapshot of the existing meta.json. Raises if the file is missing or broken."""
    doc = json.loads(meta_path(root, s.study_id, s.id).read_text(encoding="utf-8"))
    config = doc["config"]
    if not isinstance(config, dict):
        raise ValueError("meta.json: config is not an object")
    return config
