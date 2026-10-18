# syntax=docker/dockerfile:1.7
# One image for `migrate`, `api` and `worker` (PLAN §11.1).

# Build context is the repository root: `docker build -f backend/Dockerfile .`
ARG PYTHON_IMAGE=python:3.11-slim

# --- dependencies: installed from the lock file only ------------------------
FROM ${PYTHON_IMAGE} AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# --- model: URL and sha256 come from shared/models.json; build fails on a mismatch ----
FROM ${PYTHON_IMAGE} AS model
COPY shared/models.json shared/download_model.py /shared/
RUN python /shared/download_model.py face_landmarker /face_landmarker.task

# --- runtime -----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 libegl1 libgles2 \
    && rm -rf /var/lib/apt/lists/*
# Real home directory: matplotlib (imported by mediapipe) and fontconfig write caches there.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin eyetrack \
    && mkdir -p /opt/mediapipe
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY --from=model --chmod=0444 /face_landmarker.task /opt/mediapipe/face_landmarker.task
COPY backend/alembic.ini backend/cli.py backend/uvicorn_logging.json ./
COPY backend/alembic ./alembic
COPY backend/app ./app
COPY backend/worker ./worker
# worker/pipeline/features_a.py reads /shared/landmark_subset.json (same layout as the repository)
COPY shared/landmark_subset.json /shared/landmark_subset.json
# the ground truth of the moving-dot video, for `cli.py moving-dot-report`
COPY e2e/fixtures/moving_dot_path.json /shared/moving_dot_path.json
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FACE_LANDMARKER_MODEL=/opt/mediapipe/face_landmarker.task
USER eyetrack
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${API_WORKERS:-2} --log-config uvicorn_logging.json --no-access-log"]
