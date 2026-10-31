"""Time plots of an experiment's gaze against the dot: for each session and dot video, x and y over
the video's time (video px), the dot as a dark line, the gaze as measured (blue) and drift-corrected
(orange), the smooth-pursuit segments shaded. Gaps are frames without a valid gaze (blinks, face
lost). Written to `results/<experiment>/plots/<study>_<code>_<video>.png`.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from app.storage import derived_dir, session_dir  # noqa: E402
from lab.dots import read_dot_videos, truth  # noqa: E402
from lab.layout import list_sessions, results_dir, sessions_root  # noqa: E402
from lab.run import load_scores  # noqa: E402
from lab.score import DotTruth, Score  # noqa: E402
from worker.pipeline.spec import SPECS  # noqa: E402

# The reference palette of the dataviz guidance (validated: CVD and contrast pass on the surface).
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SHADE = "#efeee9"
MEASURED = "#2a78d6"
CORRECTED = "#eb6834"


def _col(table: pa.Table, name: str) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in table[name].to_pylist()], dtype=float)


def plot_video(
    gaze: pa.Table, t: DotTruth, video_id: str, title: str, subtitle: str, out: Path
) -> None:
    phase = np.asarray(gaze["phase"].to_pylist(), dtype=object)
    vids = np.asarray(gaze["video_id"].to_pylist(), dtype=object)
    valid = np.asarray(gaze["valid"].to_pylist(), dtype=bool)
    stim = _col(gaze, "stim_time")
    mine = (phase == "playback") & (vids == video_id) & np.isfinite(stim)
    order = np.argsort(stim[mine])
    s = stim[mine][order]
    ok = valid[mine][order]
    curves = {}
    for name in ("x_vid", "y_vid", "x_vid_dc", "y_vid_dc"):
        values = _col(gaze, name)[mine][order] * (t.width if name.startswith("x") else t.height)
        curves[name] = np.where(ok, values, np.nan)
    grid_t = np.arange(0.0, t.path.duration_s, 0.02)
    dot_x, dot_y = t.path.at(grid_t)

    fig, axes = plt.subplots(2, 1, figsize=(14, 6.4), sharex=True, facecolor=SURFACE)
    for ax, axis, size, dot in ((axes[0], "x", t.width, dot_x), (axes[1], "y", t.height, dot_y)):
        ax.set_facecolor(SURFACE)
        for a, b in t.pursuits:
            ax.axvspan(a, b, color=SHADE, lw=0, zorder=0)
        ax.plot(grid_t, dot * size, color=INK, lw=2.0, label="dot", zorder=3)
        ax.plot(s, curves[f"{axis}_vid"], color=MEASURED, lw=1.0, label="gaze, as measured",
                zorder=2)  # fmt: skip
        ax.plot(s, curves[f"{axis}_vid_dc"], color=CORRECTED, lw=1.0,
                label="gaze, drift-corrected", zorder=2)  # fmt: skip
        ax.set_ylim(-0.1 * size, 1.1 * size)
        ax.grid(True, color=GRID, lw=0.6)
        ax.tick_params(colors=INK_2, labelsize=9)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color(GRID)
    axes[0].set_ylabel("x (video px)", color=INK_2)
    axes[1].set_ylabel("y (video px, top = 0)", color=INK_2)
    axes[1].invert_yaxis()  # up on the plot is up on the screen
    axes[1].set_xlabel("video time (s)", color=INK_2)
    for a, b in t.pursuits:
        axes[0].text((a + b) / 2, 1.08 * t.width, "pursuit", ha="center", va="top",
                     fontsize=8, color=INK_2)  # fmt: skip
    axes[0].legend(loc="upper left", bbox_to_anchor=(0, 1.17), ncol=3, frameon=False,
                   fontsize=9, labelcolor=INK)  # fmt: skip
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK, y=0.995)
    fig.text(0.01, 0.952, subtitle, fontsize=9, color=INK_2)
    fig.tight_layout(rect=(0, 0, 1, 0.955))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110, facecolor=SURFACE)
    plt.close(fig)


def _subtitle(score: Score | None) -> str:
    if score is None:
        return ""
    dc = "-" if score.dc_pause is None else f"{score.dc_pause:.0f}"
    pursuit = "" if score.pursuit is None else f", during pursuit {score.pursuit:.0f}"
    return (
        f"median error during the rests: {score.pause:.0f} px as measured, {dc} px drift-corrected"
        f"{pursuit} (viewport px); valid frames {score.valid_fraction:.0%}"
    )


def plot_experiment(lab_data: Path, experiment: str) -> list[Path]:
    header, scores = load_scores(lab_data, experiment)
    by_key = {(s.study_id, s.code, s.video): s for s in scores if s.pipeline == "a"}
    work = results_dir(lab_data, experiment) / "data"
    studies = tuple(header.get("tune_studies", [])) + tuple(header.get("holdout_studies", []))
    written = []
    for study in studies or tuple(header.get("studies", [])):
        for ref in list_sessions(work, study):
            gaze_file = derived_dir(work, study, ref.session_id) / SPECS["a"].gaze_file
            if not gaze_file.is_file():
                continue
            gaze = pq.read_table(gaze_file)
            lab_session = session_dir(sessions_root(lab_data), study, ref.session_id)
            for d in read_dot_videos(lab_session):
                out = (
                    results_dir(lab_data, experiment) / "plots" / f"{study}_{ref.code}_{d.name}.png"
                )
                title = f"Study {study} · {ref.code} · {d.name} · experiment {experiment}"
                plot_video(gaze, truth(d.name), d.video_id, title,
                           _subtitle(by_key.get((study, ref.code, d.name))), out)  # fmt: skip
                written.append(out)
    return written
