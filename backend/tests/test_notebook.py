"""`notebooks/inspect_session.ipynb` (PLAN §12.3) runs top to bottom on a real processed session.

The notebook is executed in a real kernel (nbclient) against the fixture session after Pipeline A,
and after Pipeline B too; any cell that raises fails the test. Its session path is set through the
`INSPECT_SESSION_DERIVED` environment variable, which the kernel inherits.
"""

from pathlib import Path
from typing import Any

import nbformat
import pytest
from nbclient import NotebookClient

from app.storage import derived_dir
from tests.session_fixture import FixtureSession

NOTEBOOK_DIR = Path(__file__).resolve().parents[2] / "notebooks"
NOTEBOOK = NOTEBOOK_DIR / "inspect_session.ipynb"


def read_notebook() -> Any:
    return nbformat.read(NOTEBOOK, as_version=4)  # type: ignore[no-untyped-call]  # nbformat is untyped


def run_notebook(derived: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Execute every cell with the notebook's directory as working directory; return the outputs."""
    monkeypatch.setenv("INSPECT_SESSION_DERIVED", str(derived))
    notebook = read_notebook()
    NotebookClient(
        notebook,
        timeout=300,
        kernel_name="python3",
        resources={"metadata": {"path": str(NOTEBOOK_DIR)}},
        allow_errors=False,  # a raising cell raises CellExecutionError here
    ).execute()
    return [o for cell in notebook.cells for o in cell.get("outputs", [])]


def images(outputs: list[dict[str, Any]]) -> int:
    return sum("image/png" in o.get("data", {}) for o in outputs)


def printed(outputs: list[dict[str, Any]]) -> str:
    return "".join(o.get("text", "") for o in outputs if o["output_type"] == "stream")


def test_notebook_is_valid_and_has_no_stored_outputs() -> None:
    notebook = read_notebook()
    nbformat.validate(notebook)
    code = [c for c in notebook.cells if c.cell_type == "code"]
    assert code
    assert all(not c.outputs and c.execution_count is None for c in code)


def test_notebook_runs_on_a_session_after_pipeline_a(
    a_session: FixtureSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    derived = derived_dir(a_session.root, a_session.study_id, a_session.session_id)
    outputs = run_notebook(derived, monkeypatch)
    text = printed(outputs)
    assert f"session {a_session.session_id}" in text
    assert "included:" in text
    assert images(outputs) >= 3  # gaze, calibration and validation at least
    assert "Pipeline B has not run" in text


def test_notebook_draws_pipeline_b_next_to_a(
    b_session: FixtureSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    derived = derived_dir(b_session.root, b_session.study_id, b_session.session_id)
    outputs = run_notebook(derived, monkeypatch)
    assert "Pipeline B has not run" not in printed(outputs)
    assert images(outputs) >= 6  # A's three main plots and B's gaze, calibration and validation
