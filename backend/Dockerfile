# syntax=docker/dockerfile:1.7
# One image for `migrate`, `api` and `worker` (PLAN §11.1).

ARG PYTHON_IMAGE=python:3.11-slim
# Pinned face_landmarker.task (source and hash recorded in docs/DECISIONS.md).
ARG FACE_LANDMARKER_URL=https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
ARG FACE_LANDMARKER_SHA256=64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff

# --- dependencies: installed from the lock file only ------------------------
FROM ${PYTHON_IMAGE} AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# --- model: build fails if the sha256 does not match ------------------------
FROM ${PYTHON_IMAGE} AS model
ARG FACE_LANDMARKER_URL
ARG FACE_LANDMARKER_SHA256
ADD --checksum=sha256:${FACE_LANDMARKER_SHA256} ${FACE_LANDMARKER_URL} /face_landmarker.task

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
COPY alembic.ini cli.py uvicorn_logging.json ./
COPY alembic ./alembic
COPY app ./app
COPY worker ./worker
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FACE_LANDMARKER_MODEL=/opt/mediapipe/face_landmarker.task
USER eyetrack
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${API_WORKERS:-2} --log-config uvicorn_logging.json --no-access-log"]
