"""Measured simulator scheduling; required regression checks are never dropped."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable

from .runtime import TimeLimitError


def _seconds(value: float, fallback: float = 1.0) -> float:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else fallback


def parallel_seconds(costs: list[float], workers: int = 2) -> float:
    """Estimate the same bounded FIFO worker queue used by the controller."""
    slots = [0.0] * workers
    for cost in costs:
        index = min(range(workers), key=slots.__getitem__)
        slots[index] += _seconds(cost)
    return max(slots, default=0.0)


@dataclass(frozen=True)
class ExperimentSelection:
    experiments: list[dict]
    required_count: int
    optional_count: int
    omitted_count: int
    estimated_seconds: float
    available_seconds: float

    def summary(self) -> dict:
        return {"required": self.required_count, "additional": self.optional_count,
                "additional_omitted": self.omitted_count,
                "estimated_seconds": round(self.estimated_seconds, 3),
                "available_seconds": round(self.available_seconds, 3)}


def select_experiments(required: list[dict], optional: list[dict], *,
                       costs: Callable[[dict], tuple[float, float]],
                       priority: Callable[[dict], tuple],
                       available_seconds: float) -> ExperimentSelection:
    """Fit extra coverage after all public, explicit and retained obligations."""
    mandatory = sorted(required, key=priority)
    extras = sorted(optional, key=priority)
    measured = {id(item): costs(item) for item in [*mandatory, *extras]}

    def estimate(items: list[dict]) -> float:
        # Accepted and candidate matrices execute in separate phases. Account
        # for cached results before estimating their two-worker queues.
        before = parallel_seconds([measured[id(item)][0] for item in items])
        after = parallel_seconds([measured[id(item)][1] for item in items])
        return (before + after) * 1.25 + (2.0 if items else 0.0)

    if estimate(mandatory) > available_seconds:
        raise TimeLimitError("insufficient time for required public and retained experiments")
    selected = list(mandatory)
    added = 0
    for item in extras:
        if estimate([*selected, item]) <= available_seconds:
            selected.append(item)
            added += 1
    return ExperimentSelection(selected, len(mandatory), added, len(extras) - added,
                               estimate(selected), available_seconds)
