"""What each processing stage did in its last run: `derived/stages.json` (PLAN §8, §12.2).

One entry per stage that has run: `status` (`ok`, `skipped` or `failed`), `seconds`, the `summary`
the stage returned and, for a failed stage, the `error`. The runner writes the file after every
stage, also when a stage fails, so the admin's stage table can say where a session stopped. A stage
that has not run (yet) has no entry. Light module: the api process reads it too.
"""

import json
from pathlib import Path
from typing import Any

from app.storage import atomic_write_bytes

STAGES_FILE = "stages.json"
STAGES_VERSION = 1

Entries = dict[str, dict[str, Any]]


def status_of(summary: Any) -> str:
    """`skipped` when a stage's summary says it was skipped, else `ok`."""
    return "skipped" if isinstance(summary, dict) and "skipped" in summary else "ok"


def read_entries(
    derived: Path, quality: dict[str, Any] | None = None, filename: str = STAGES_FILE
) -> Entries:
    """The entries of `stages.json`. Without that file (a session processed before it existed) the
    stages recorded in the quality report, if given, with their status worked out."""
    path = derived / filename
    if path.is_file():
        stages = json.loads(path.read_text(encoding="utf-8")).get("stages", {})
        return dict(stages) if isinstance(stages, dict) else {}
    recorded = (quality or {}).get("stages") or {}
    return {
        name: {
            "status": status_of(entry.get("summary")),
            "seconds": entry.get("seconds"),
            "summary": entry.get("summary"),
            "error": None,
        }
        for name, entry in recorded.items()
    }


def write_entries(derived: Path, entries: Entries, filename: str = STAGES_FILE) -> None:
    document = {"version": STAGES_VERSION, "stages": entries}
    atomic_write_bytes(derived / filename, (json.dumps(document, indent=1) + "\n").encode())
