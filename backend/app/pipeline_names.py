"""Names of the processing stages and the debug plots, in one light module.

The admin pages (api process) need them without importing the pipeline (MediaPipe, matplotlib);
`worker.pipeline.runner` and `worker.pipeline.plots` take them from here (PLAN §1: single source).
"""

# In the order the runner runs them (PLAN §8, §12.2).
STAGE_NAMES = (
    "ingest",
    "landmarks",
    "features",
    "sync",
    "calibration",
    "gaze",
    "quality",
    "plots",
)

# What each stage does, for the admin stage table.
STAGE_DESCRIPTIONS = {
    "ingest": "check the chunks, assemble the webcam video and the streams",
    "landmarks": "face landmarks of every webcam frame (MediaPipe)",
    "features": "Pipeline A features and blinks",
    "sync": "webcam time to client time",
    "calibration": "fit the gaze model on the calibration dots",
    "gaze": "predict, smooth, fixations, video coordinates",
    "quality": "quality report and inclusion decision",
    "plots": "debug plots",
}

# The debug plots in `derived/debug/`, with a caption for the admin page.
PLOT_CAPTIONS = {
    "calibration.png": "Calibration attempts: targets and the model's median predictions",
    "validation.png": "Validation error per block and drift",
    "sync.png": "Sync: correlation curve and drift fit",
    "signals.png": "Quality signals over the session",
    "gaze.png": "Gaze over time",
}
PLOT_FILES = tuple(PLOT_CAPTIONS)

# Pipeline B's plots (stage `plots_b`, PLAN §12.2): the ones of A that depend on the pipeline. The
# sync and quality-signal plots do not, so they are not repeated.
PLOT_B_CAPTIONS = {
    "calibration_b.png": "Pipeline B calibration: targets and the model's median predictions",
    "validation_b.png": "Pipeline B validation error per block and drift",
    "gaze_b.png": "Pipeline B gaze over time",
}
PLOT_B_FILES = tuple(PLOT_B_CAPTIONS)

# The debug replay video (`derived/debug/replay.mp4`), rendered on request (PLAN §12.2). It contains
# the participant's webcam video, so it is only served to a logged-in admin.
REPLAY_FILE = "replay.mp4"
