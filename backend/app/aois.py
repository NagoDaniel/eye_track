"""Areas of interest of a video (PLAN §9.3): the `aois.json` schema and the hit test.

Coordinates are video-normalized (0 to 1 over the content rectangle). The same models validate what
the admin saves and what the analysis reads, so the two cannot disagree (PLAN §1).
"""

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.service_errors import InvalidError

Id = Annotated[str, Field(min_length=1)]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Coord = Annotated[float, Field(allow_inf_nan=False)]
Point = Annotated[list[Coord], Field(min_length=2, max_length=2)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Segment(_Strict):
    id: Id
    start: Seconds
    end: Seconds

    @model_validator(mode="after")
    def _ordered(self) -> "Segment":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class _AoiBase(_Strict):
    id: Id
    label: str
    start: Seconds
    end: Seconds

    @model_validator(mode="after")
    def _ordered(self) -> "_AoiBase":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class RectAoi(_AoiBase):
    shape: Literal["rect"]
    coords: Annotated[list[Coord], Field(min_length=4, max_length=4)]  # x0, y0, x1, y1

    @model_validator(mode="after")
    def _corners(self) -> "RectAoi":
        x0, y0, x1, y1 = self.coords
        if x1 <= x0 or y1 <= y0:
            raise ValueError("coords must be [x0, y0, x1, y1] with x1 > x0 and y1 > y0")
        return self


class PolygonAoi(_AoiBase):
    shape: Literal["polygon"]
    coords: Annotated[list[Point], Field(min_length=3)]


Aoi = Annotated[RectAoi | PolygonAoi, Field(discriminator="shape")]


class AoiFile(_Strict):
    segments: list[Segment] = []
    aois: list[Aoi] = []

    @model_validator(mode="after")
    def _unique_ids(self) -> "AoiFile":
        for name, items in (("segments", self.segments), ("aois", self.aois)):
            ids = [i.id for i in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{name} ids must be unique")
        return self


def parse_aoi_file(value: dict[str, Any]) -> AoiFile:
    """Validate an `aois.json` value; `InvalidError` names the first problems."""
    try:
        return AoiFile.model_validate(value)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'aois'}: {e['msg']}" for e in exc.errors()[:3]
        )
        raise InvalidError(f"aois is not valid: {problems}") from exc


def aoi_active(aoi: RectAoi | PolygonAoi, stim_time: float) -> bool:
    """Whether the AOI exists at this media time (from its start, up to but excluding its end)."""
    return aoi.start <= stim_time < aoi.end


def _segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    u = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + u * dx), py - (ay + u * dy))


def _inside_polygon(x: float, y: float, points: list[list[float]]) -> bool:
    inside = False
    for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1], strict=True):
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            inside = not inside
    return inside


def aoi_contains(aoi: RectAoi | PolygonAoi, x: float, y: float, tolerance: float = 0.0) -> bool:
    """Whether the point is in the shape grown by `tolerance` (a distance, so corners are round).
    Points on the border count as inside."""
    if isinstance(aoi, RectAoi):
        x0, y0, x1, y1 = aoi.coords
        dx = max(x0 - x, 0.0, x - x1)
        dy = max(y0 - y, 0.0, y - y1)
        return math.hypot(dx, dy) <= tolerance
    points = aoi.coords
    if _inside_polygon(x, y, points):
        return True
    edges = zip(points, points[1:] + points[:1], strict=True)
    return any(_segment_distance(x, y, a[0], a[1], b[0], b[1]) <= tolerance for a, b in edges)
