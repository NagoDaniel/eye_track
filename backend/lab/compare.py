"""Compare an experiment with a base (normally `baseline`), session by session, and apply the
acceptance rule agreed for T13 (DECISIONS 2026-10-30, item 4):

- better: the median over the tuning sessions of the metric is lower than the base's, and
- no session (tuning or held-out) is worse than the base by more than `MAX_WORSE_FRACTION`, and
- the held-out sessions' median is not worse than the base's.

The metric is a field of `Score` (table 1, viewport px): `dc_pause` (drift-corrected, during the
dot's rests: what the analyses use, and accuracy without the lag) and `pause` (as measured).
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from lab.score import Score

MAX_WORSE_FRACTION = 0.10
METRICS = ("dc_pause", "pause", "dc_median", "median", "move")
VERDICT_METRICS = ("dc_pause", "pause")

Key = tuple[int, str, str]  # study, code, pipeline


@dataclass(frozen=True)
class Row:
    key: Key
    base: float | None
    new: float | None

    @property
    def change(self) -> float | None:
        if self.base is None or self.new is None or self.base == 0:
            return None
        return (self.new - self.base) / self.base


@dataclass(frozen=True)
class Verdict:
    metric: str
    pipeline: str
    tune_base: float
    tune_new: float
    holdout_base: float | None
    holdout_new: float | None
    worse: list[Row]  # sessions worse than the base by more than MAX_WORSE_FRACTION
    missing: list[Key]  # in the base but without a score in the new run
    better: bool


def _value(score: Score, metric: str) -> float | None:
    value = getattr(score, metric)
    return None if value is None else float(value)


def rows(base: Sequence[Score], new: Sequence[Score], metric: str) -> list[Row]:
    by_key = {(s.study_id, s.code, s.pipeline): s for s in new}
    out = []
    for s in base:
        key = (s.study_id, s.code, s.pipeline)
        other = by_key.get(key)
        out.append(Row(key, _value(s, metric), None if other is None else _value(other, metric)))
    return out


def _median(values: Sequence[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return float(np.median(known)) if known else None


def verdict(
    base: Sequence[Score],
    new: Sequence[Score],
    metric: str,
    pipeline: str,
    tune: Sequence[int],
    holdout: Sequence[int],
) -> Verdict:
    mine = [r for r in rows(base, new, metric) if r.key[2] == pipeline]
    missing = [r.key for r in mine if r.base is not None and r.new is None]
    in_tune = [r for r in mine if r.key[0] in tune]
    in_hold = [r for r in mine if r.key[0] in holdout]
    tune_base, tune_new = _median([r.base for r in in_tune]), _median([r.new for r in in_tune])
    hold_base, hold_new = _median([r.base for r in in_hold]), _median([r.new for r in in_hold])
    worse = [r for r in mine if r.change is not None and r.change > MAX_WORSE_FRACTION]
    if tune_base is None or tune_new is None:
        raise ValueError(f"no tuning sessions with a '{metric}' score for pipeline {pipeline}")
    better = (
        tune_new < tune_base
        and not worse
        and not missing
        and (hold_base is None or (hold_new is not None and hold_new <= hold_base))
    )
    return Verdict(metric, pipeline, tune_base, tune_new, hold_base, hold_new, worse, missing,
                   better)  # fmt: skip


def _n(v: float | None, spec: str = "6.0f") -> str:
    return f"{'-':>6}" if v is None else f"{v:{spec}}"


def format_comparison(
    base_name: str,
    base: Sequence[Score],
    new_name: str,
    new: Sequence[Score],
    tune: Sequence[int],
    holdout: Sequence[int],
) -> str:
    lines = [f"{new_name} against {base_name} (viewport px; change = new / base - 1)", ""]
    lines.append(f"{'':20}" + "".join(f" | {m:^18}" for m in METRICS))
    lines.append(f"{'study':>5} {'code':8}{'pl':3}   " + " |   base    new  chg" * len(METRICS))
    tables = {m: {r.key: r for r in rows(base, new, m)} for m in METRICS}
    for s in base:
        key = (s.study_id, s.code, s.pipeline)
        line = f"{s.study_id:5} {s.code:8}{s.pipeline:3}   "
        for m in METRICS:
            r = tables[m][key]
            change = "   -" if r.change is None else f"{r.change * 100:+3.0f}%"
            line += f" | {_n(r.base)} {_n(r.new)} {change}"
        lines.append(line)
    lines += ["", f"verdict (tune {list(tune)}, held out {list(holdout)}, no session worse than "
              f"+{MAX_WORSE_FRACTION:.0%}):"]  # fmt: skip
    for p in sorted({s.pipeline for s in base}):
        for m in VERDICT_METRICS:
            try:
                v = verdict(base, new, m, p, tune, holdout)
            except ValueError as e:
                lines.append(f"  {p.upper()} {m}: {e}")
                continue
            worse = ", ".join(f"{k[1]}({k[0]}) {r.change * 100:+.0f}%" for r in v.worse
                              for k in [r.key] if r.change is not None)  # fmt: skip
            lines.append(
                f"  {p.upper()} {m:9} tune median {v.tune_base:.0f} -> {v.tune_new:.0f}, "
                f"held out {_n(v.holdout_base, '.0f')} -> {_n(v.holdout_new, '.0f')}"
                f"{', worse: ' + worse if worse else ''}"
                f"{', missing: ' + str(v.missing) if v.missing else ''}"
                f"  => {'BETTER' if v.better else 'not better'}"
            )
    return "\n".join(lines) + "\n"
