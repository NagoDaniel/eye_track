"""An experiment: a named `PipelineConfig` (overrides of the defaults) and the sessions it runs on.

TOML, in `backend/lab/experiments/`:

    name = "baseline"
    description = "the product pipeline as it is"
    pipelines = ["a", "b"]          # B runs where the session has cached features_b
    tune_studies = [7]              # the studies a parameter may be chosen on
    holdout_studies = [6]           # the studies that only check the choice

    [config]                        # PipelineConfig fields; anything else is an error
    calib_alphas = [1.0, 10.0]

Unknown keys, unknown config fields and values of the wrong type are refused (fail loudly): a typo
must not run the baseline under another name.
"""

import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from worker.pipeline.config import PipelineConfig

PIPELINES = ("a", "b")
_TOP_KEYS = {"name", "description", "pipelines", "tune_studies", "holdout_studies", "config"}


class ExperimentError(Exception):
    """The experiment file is wrong; the message says where."""


@dataclass(frozen=True)
class Experiment:
    name: str
    description: str
    pipelines: tuple[str, ...]
    tune_studies: tuple[int, ...]
    holdout_studies: tuple[int, ...]
    config: PipelineConfig
    overrides: dict[str, Any]  # what the file changed, for the report

    @property
    def studies(self) -> tuple[int, ...]:
        return self.tune_studies + self.holdout_studies


def _int(name: str, value: int | float) -> int:
    if isinstance(value, float):
        raise ExperimentError(f"config.{name}: {value!r} must be a whole number")
    return value


def _coerce(name: str, default: Any, value: Any) -> Any:
    """`value` in the type of the field's default (int is fine for a float, a list for a tuple)."""
    number = isinstance(value, int | float) and not isinstance(value, bool)
    if isinstance(default, bool) and isinstance(value, bool):
        return value
    if isinstance(default, float) and number:
        return float(value)
    if isinstance(default, int) and not isinstance(default, bool) and number:
        return _int(name, value)
    if isinstance(default, str) and isinstance(value, str):
        return value
    if isinstance(default, tuple) and default and isinstance(value, list) and value:
        return tuple(_coerce(name, default[0], v) for v in value)
    raise ExperimentError(
        f"config.{name}: {value!r} is not a {type(default).__name__} like the default {default!r}"
    )


def config_with(overrides: dict[str, Any]) -> PipelineConfig:
    base = PipelineConfig()
    known = {f.name for f in fields(PipelineConfig)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise ExperimentError(f"unknown PipelineConfig fields: {', '.join(unknown)}")
    values = {k: _coerce(k, getattr(base, k), v) for k, v in overrides.items()}
    return replace(base, **values)


def _ints(data: dict[str, Any], key: str) -> tuple[int, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(v, int) and not isinstance(v, bool) for v in value
    ):
        raise ExperimentError(f"{key} must be a list of study ids")
    return tuple(value)


def parse_experiment(data: dict[str, Any]) -> Experiment:
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise ExperimentError(f"unknown keys: {', '.join(unknown)}")
    name = data.get("name")
    if not isinstance(name, str) or not name.replace("-", "").replace("_", "").isalnum():
        raise ExperimentError("name must be letters, digits, '-' or '_'")
    pipelines = data.get("pipelines", ["a"])
    if not isinstance(pipelines, list) or not pipelines or set(pipelines) - set(PIPELINES):
        raise ExperimentError(f"pipelines must be a non-empty list of {PIPELINES}")
    tune, holdout = _ints(data, "tune_studies"), _ints(data, "holdout_studies")
    if not tune:
        raise ExperimentError("tune_studies must name at least one study")
    if set(tune) & set(holdout):
        raise ExperimentError("a study cannot be both a tuning and a held-out study")
    overrides = data.get("config", {})
    if not isinstance(overrides, dict):
        raise ExperimentError("config must be a table")
    return Experiment(
        name=name,
        description=str(data.get("description", "")),
        pipelines=tuple(pipelines),
        tune_studies=tune,
        holdout_studies=holdout,
        config=config_with(overrides),
        overrides=dict(overrides),
    )


def load_experiment(path: Path) -> Experiment:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ExperimentError(f"{path}: {e}") from e
    try:
        return parse_experiment(data)
    except ExperimentError as e:
        raise ExperimentError(f"{path}: {e}") from e
