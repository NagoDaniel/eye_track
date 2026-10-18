"""Small real videos made with the real ffmpeg, for tests that need a file to play."""

import subprocess
from pathlib import Path


def make_video(
    path: Path, seconds: float = 3.0, size: str = "320x240", fps: int = 25, codec: str = "libx264"
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-pix_fmt", "yuv420p", "-c:v", codec]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"testsrc2=duration={seconds}:size={size}:rate={fps}", *args, str(path)],
        check=True,
    )  # fmt: skip
    return path


def probe(path: Path) -> dict[str, str]:
    """Codec, pixel format, size, frame rate and duration of the first video stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-of", "default=nw=1", "-show_entries",
         "stream=codec_name,pix_fmt,width,height,r_frame_rate,duration", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
