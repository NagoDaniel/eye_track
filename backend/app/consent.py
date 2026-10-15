"""Consent texts: one plain-text file per version in `app/consent/<version>.txt` (DECISIONS 1b)."""

import re
from pathlib import Path

CONSENT_DIR = Path(__file__).resolve().parent / "consent"
_VERSION_RE = re.compile(r"[A-Za-z0-9._-]+")


class UnknownConsentVersion(ValueError):
    pass


def consent_text(version: str) -> str:
    """Text of a consent version. Raises UnknownConsentVersion if there is no such file."""
    if not _VERSION_RE.fullmatch(version) or version.startswith("."):
        raise UnknownConsentVersion(f"invalid consent version {version!r}")
    path = CONSENT_DIR / f"{version}.txt"
    if not path.is_file():
        raise UnknownConsentVersion(f"no consent text for version {version!r}")
    return path.read_text(encoding="utf-8")


def consent_versions() -> list[str]:
    return sorted(p.stem for p in CONSENT_DIR.glob("*.txt"))
