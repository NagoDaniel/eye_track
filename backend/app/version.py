"""The version of the running code: the git hash the image was built from."""

import os


def git_hash() -> str | None:
    """The git hash the image was built from (`GIT_SHA` build argument), None when not known."""
    value = os.environ.get("GIT_SHA", "").strip()
    return value or None
