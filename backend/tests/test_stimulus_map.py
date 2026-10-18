"""Client time to stimulus time (PLAN §8.2 step 5, §13: mapping across pause and stall gaps)."""

import numpy as np
import pytest

from app.events import parse_events, parse_records
from app.schemas import EventRecord, StimulusFrameRecord
from tests.session_fixture import FIXTURE
from worker.pipeline.config import PipelineConfig
from worker.pipeline.stimulus_map import StimulusMap

CONFIG = PipelineConfig()


def frame(e: float, m: float) -> StimulusFrameRecord:
    return StimulusFrameRecord(t=e + 5, expected_display_time=e, media_time=m, presented_frames=1)


def event(t: float, type_: str, video_id: str) -> EventRecord:
    return EventRecord.model_validate({"t": t, "type": type_, "data": {"video_id": video_id}})


def steady(e0: float, m0: float, n: int, step_ms: float = 40.0) -> list[StimulusFrameRecord]:
    return [frame(e0 + i * step_ms, m0 + i * step_ms / 1000) for i in range(n)]


def lookup(
    frames: list[StimulusFrameRecord], events: list[EventRecord], t: list[float]
) -> tuple[list[str | None], list[float]]:
    result = StimulusMap(frames, events, CONFIG).lookup(np.array(t))
    return result.video_id, [float(x) for x in result.stim_time]


ONE_VIDEO = [event(900, "video_load", "1"), event(5000, "video_ended", "1")]


def test_a_frame_time_maps_to_its_media_time_and_between_frames_it_is_interpolated() -> None:
    frames = steady(1000, 0.0, 50)
    ids, times = lookup(frames, ONE_VIDEO, [1000, 1040, 1020, 1030, 2000.0])
    assert ids == ["1"] * 5
    assert times == pytest.approx([0.0, 0.04, 0.02, 0.03, 1.0])


def test_display_jitter_is_not_a_gap() -> None:
    # A 25 fps video on a display that shows frames 33 or 50 ms apart: the media time still
    # advances 40 ms per frame, so the mapping is continuous.
    e, m, frames = 1000.0, 0.0, []
    for i in range(40):
        frames.append(frame(e, m))
        e += 33.4 if i % 3 else 50.1
        m += 0.04
    ids, times = lookup(
        frames,
        ONE_VIDEO,
        [(frames[10].expected_display_time + frames[11].expected_display_time) / 2],
    )
    assert ids == ["1"] and times == pytest.approx([0.42])


def test_dropped_frames_are_not_a_gap() -> None:
    frames = [*steady(1000, 0.0, 10), *steady(1000 + 9 * 40 + 120, 9 * 0.04 + 0.12, 10)]
    _, times = lookup(frames, ONE_VIDEO, [1000 + 9 * 40 + 60])  # in the middle of the 120 ms step
    assert times == pytest.approx([9 * 0.04 + 0.06])


def test_a_pause_gives_no_stimulus_time() -> None:
    before = steady(1000, 0.0, 26)  # up to e=2000, media 1.0
    after = steady(4000, 1.04, 20)  # resumed 2 s later; the media time moved on by one frame
    ids, times = lookup([*before, *after], ONE_VIDEO, [1500, 2000, 2500, 3999, 4000, 4040])
    assert ids == ["1"] * 6  # the video is on screen the whole time
    assert times[0] == pytest.approx(0.5)
    assert all(np.isnan(times[1:4])) and np.isnan(times[3])
    assert times[4] == pytest.approx(1.04) and times[5] == pytest.approx(1.08)


def test_a_stall_gives_no_stimulus_time_and_a_seek_back_is_a_gap_too() -> None:
    stalled = [*steady(1000, 0.0, 10), *steady(1000 + 9 * 40 + 300, 9 * 0.04 + 0.04, 10)]
    _, times = lookup(stalled, ONE_VIDEO, [1000 + 9 * 40 + 150])
    assert np.isnan(times[0])
    backwards = [*steady(1000, 0.0, 10), *steady(1400, 0.0, 10)]
    _, times = lookup(backwards, ONE_VIDEO, [1000 + 9 * 40 + 20])
    assert np.isnan(times[0])


def test_two_videos_have_their_own_media_time_and_nothing_maps_between_them() -> None:
    events = [
        event(900, "video_load", "1"), event(1700, "video_ended", "1"),
        event(2400, "video_load", "2"), event(3500, "video_ended", "2"),
    ]  # fmt: skip
    frames = [*steady(1000, 0.0, 17), *steady(3000, 0.0, 12)]
    ids, times = lookup(frames, events, [500, 950, 1400, 1800, 2500, 2999, 3200, 3600])
    assert ids == [None, None, "1", None, None, None, "2", None]
    assert times[2] == pytest.approx(0.4) and times[6] == pytest.approx(0.2)
    assert all(np.isnan(times[i]) for i in (0, 1, 3, 4, 5, 7))


def test_after_the_last_frame_one_frame_is_extrapolated_and_the_end_event_closes_the_video() -> (
    None
):
    frames = steady(1000, 0.0, 10)  # last frame at e=1360, media 0.36
    ids, times = lookup(frames, [event(900, "video_load", "1")], [1380, 1500, 9000])
    assert ids[0] == "1" and times[0] == pytest.approx(0.38)
    assert np.isnan(times[1]) and np.isnan(times[2])
    ids, _ = lookup(
        frames, [event(900, "video_load", "1"), event(1370, "video_ended", "1")], [1365, 1380]
    )
    assert ids == ["1", None]


def test_unordered_and_repeated_frames_and_no_frames() -> None:
    frames = steady(1000, 0.0, 5)
    shuffled = [frames[3], frames[0], frames[1], frames[1], frames[4], frames[2]]
    _, times = lookup(shuffled, ONE_VIDEO, [1100])
    assert times == pytest.approx([0.1])
    ids, times = lookup([], ONE_VIDEO, [1000, 2000])
    assert ids == [None, None] and all(np.isnan(times))


def test_frames_before_any_video_load_fail_loudly() -> None:
    with pytest.raises(ValueError, match="video_load"):
        StimulusMap(steady(1000, 0.0, 5), [], CONFIG)


def test_the_recorded_session() -> None:
    raw = FIXTURE / "raw"
    frames = parse_records(
        (raw / "stimulus_frames.jsonl").read_bytes(), StimulusFrameRecord, "frames"
    )
    events = parse_events((raw / "events.jsonl").read_bytes())
    ended = next(e.t for e in events if e.type == "video_ended")
    first = frames[0].expected_display_time
    result = StimulusMap(frames, events, CONFIG).lookup(
        np.array([f.expected_display_time for f in frames])
    )
    # Every presented frame maps to its own media time, in order, with no gap in a smooth playback.
    assert result.video_id == ["1"] * len(frames)
    assert result.stim_time == pytest.approx([f.media_time for f in frames])
    # Between two frames the time is between their media times; outside the video it is null.
    mid = (frames[20].expected_display_time + frames[21].expected_display_time) / 2
    ids, times = lookup(frames, events, [first - 5, mid, ended + 50])
    assert ids == [None, "1", None]
    assert frames[20].media_time < times[1] < frames[21].media_time
    assert np.isnan(times[0]) and np.isnan(times[2])
    # The video lasted its 3 s (the test clip) within the stimulus frames.
    assert frames[-1].media_time == pytest.approx(2.96)
