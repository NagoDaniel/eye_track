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
