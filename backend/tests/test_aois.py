"""AOI schema and hit tests (PLAN §9.3, §13)."""

import math
from typing import Any

import pytest
from pydantic import TypeAdapter

from app.aois import Aoi, AoiFile, PolygonAoi, RectAoi, aoi_active, aoi_contains, parse_aoi_file
from app.service_errors import InvalidError

PLAN_EXAMPLE: dict[str, Any] = {
    "segments": [{"id": "intro", "start": 0.0, "end": 32.5}],
    "aois": [
        {"id": "code_panel", "label": "Code", "start": 12.0, "end": 95.0,
         "shape": "rect", "coords": [0.55, 0.10, 0.98, 0.90]},
        {"id": "dancers", "label": "Dancers", "start": 0.0, "end": 180.0,
         "shape": "polygon", "coords": [[0.1, 0.2], [0.5, 0.2], [0.5, 0.9], [0.1, 0.9]]},
    ],
}  # fmt: skip


ADAPTER: TypeAdapter[RectAoi | PolygonAoi] = TypeAdapter(Aoi)


def rect(coords: list[float], start: float = 0.0, end: float = 10.0) -> RectAoi | PolygonAoi:
    return ADAPTER.validate_python(
        {"id": "r", "label": "R", "start": start, "end": end, "shape": "rect", "coords": coords}
    )


def polygon(coords: list[list[float]]) -> RectAoi | PolygonAoi:
    return ADAPTER.validate_python(
        {"id": "p", "label": "P", "start": 0, "end": 10, "shape": "polygon", "coords": coords}
    )


def test_plan_example_parses_and_roundtrips() -> None:
    parsed = parse_aoi_file(PLAN_EXAMPLE)
    assert [a.id for a in parsed.aois] == ["code_panel", "dancers"]
    assert parsed.segments[0].end == 32.5
    assert parse_aoi_file(parsed.model_dump(mode="json")) == parsed


def test_empty_object_is_valid_and_has_nothing() -> None:
    assert parse_aoi_file({}) == AoiFile(segments=[], aois=[])


@pytest.mark.parametrize(
    "bad, reason",
    [
        ({"aois": [{"id": "a", "label": "A", "start": 5, "end": 5, "shape": "rect",
                    "coords": [0, 0, 1, 1]}]}, "end"),
        ({"aois": [{"id": "a", "label": "A", "start": 0, "end": 5, "shape": "rect",
                    "coords": [0.5, 0, 0.2, 1]}]}, "coords"),
        ({"aois": [{"id": "a", "label": "A", "start": 0, "end": 5, "shape": "rect",
                    "coords": [0, 0, 1]}]}, "coords"),
        ({"aois": [{"id": "a", "label": "A", "start": 0, "end": 5, "shape": "polygon",
                    "coords": [[0, 0], [1, 1]]}]}, "coords"),
        ({"aois": [{"id": "a", "label": "A", "start": 0, "end": 5, "shape": "circle",
                    "coords": [0, 0, 1]}]}, "shape"),
        ({"aois": [{"id": "a", "label": "A", "start": -1, "end": 5, "shape": "rect",
                    "coords": [0, 0, 1, 1]}]}, "start"),
        ({"aois": [{"id": "a", "label": "A", "start": 0, "end": 5, "shape": "rect",
                    "coords": [0, 0, 1, 1]}] * 2}, "unique"),
        ({"segments": [{"id": "s", "start": 3, "end": 1}]}, "end"),
        ({"segments": [{"id": "s", "start": 0, "end": 1}] * 2}, "unique"),
        ({"unknown": 1}, "unknown"),
        ({"aois": [{"id": "", "label": "A", "start": 0, "end": 5, "shape": "rect",
                    "coords": [0, 0, 1, 1]}]}, "id"),
    ],
)  # fmt: skip
def test_bad_aois_are_refused_with_a_reason(bad: dict[str, Any], reason: str) -> None:
    with pytest.raises(InvalidError) as err:
        parse_aoi_file(bad)
    assert reason in str(err.value).lower()


def test_rect_edges_are_inside() -> None:
    r = rect([0.2, 0.3, 0.6, 0.7])
    assert aoi_contains(r, 0.2, 0.3) and aoi_contains(r, 0.6, 0.7) and aoi_contains(r, 0.4, 0.5)
    assert not aoi_contains(r, 0.19, 0.5) and not aoi_contains(r, 0.4, 0.71)


def test_polygon_inside_edge_vertex_outside() -> None:
    square = polygon([[0.1, 0.2], [0.5, 0.2], [0.5, 0.9], [0.1, 0.9]])
    assert aoi_contains(square, 0.3, 0.5)
    assert aoi_contains(square, 0.1, 0.5)  # on an edge
    assert aoi_contains(square, 0.5, 0.9)  # on a vertex
    assert not aoi_contains(square, 0.55, 0.5) and not aoi_contains(square, 0.3, 0.1)


def test_concave_polygon_notch_is_outside() -> None:
    # a "U": the notch between x 0.4 and 0.6, from y 0.5 up, is not part of the shape
    u = polygon(
        [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.6, 1.0], [0.6, 0.5], [0.4, 0.5], [0.4, 1.0],
         [0.0, 1.0]]
    )  # fmt: skip
    assert aoi_contains(u, 0.1, 0.9) and aoi_contains(u, 0.5, 0.2) and aoi_contains(u, 0.9, 0.9)
    assert not aoi_contains(u, 0.5, 0.9)


def test_tolerance_grows_the_shape_by_a_distance() -> None:
    r = rect([0.2, 0.3, 0.6, 0.7])
    assert not aoi_contains(r, 0.65, 0.5)
    assert aoi_contains(r, 0.65, 0.5, tolerance=0.0501)
    assert not aoi_contains(r, 0.65, 0.5, tolerance=0.049)
    # at a corner the grown shape is rounded: distance, not a bigger rectangle
    corner = math.hypot(0.04, 0.04)
    assert aoi_contains(r, 0.64, 0.74, tolerance=corner + 1e-9)
    assert not aoi_contains(r, 0.64, 0.74, tolerance=0.045)
    u = polygon([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    assert aoi_contains(u, 1.03, 0.5, tolerance=0.05) and not aoi_contains(u, 1.06, 0.5, 0.05)


def test_active_interval_is_closed_at_start_open_at_end() -> None:
    r = rect([0, 0, 1, 1], start=2.0, end=4.0)
    assert not aoi_active(r, 1.99) and aoi_active(r, 2.0) and aoi_active(r, 3.9)
    assert not aoi_active(r, 4.0)
