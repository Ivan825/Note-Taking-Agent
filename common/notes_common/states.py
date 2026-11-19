"""Job lifecycle (spec section 4): statuses, allowed transitions and progress mapping."""

from __future__ import annotations

from enum import Enum


class Status(str, Enum):
    QUEUED = "QUEUED"
    TRANSCRIBING = "TRANSCRIBING"
    SUMMARIZING = "SUMMARIZING"
    EXTRACTING_FRAMES = "EXTRACTING_FRAMES"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


PIPELINE: list[Status] = [
    Status.TRANSCRIBING,
    Status.SUMMARIZING,
    Status.EXTRACTING_FRAMES,
    Status.PUBLISHING,
]

TERMINAL: frozenset[Status] = frozenset(
    {Status.COMPLETED, Status.COMPLETED_WITH_WARNINGS, Status.FAILED, Status.DEAD_LETTERED}
)

_FAIL = {Status.FAILED, Status.DEAD_LETTERED}

ALLOWED: dict[Status, set[Status]] = {
    Status.QUEUED: {Status.TRANSCRIBING} | _FAIL,
    Status.TRANSCRIBING: {Status.SUMMARIZING} | _FAIL,
    Status.SUMMARIZING: {Status.EXTRACTING_FRAMES} | _FAIL,
    Status.EXTRACTING_FRAMES: {Status.PUBLISHING} | _FAIL,
    Status.PUBLISHING: {Status.COMPLETED, Status.COMPLETED_WITH_WARNINGS} | _FAIL,
    Status.COMPLETED: set(),
    Status.COMPLETED_WITH_WARNINGS: set(),
    Status.FAILED: set(),
    Status.DEAD_LETTERED: set(),
}

# Baseline progress (%) when a stage starts; stages may report finer progress within their band.
PROGRESS: dict[Status, int] = {
    Status.QUEUED: 0,
    Status.TRANSCRIBING: 10,
    Status.SUMMARIZING: 30,
    Status.EXTRACTING_FRAMES: 50,
    Status.PUBLISHING: 85,
    Status.COMPLETED: 100,
    Status.COMPLETED_WITH_WARNINGS: 100,
    Status.FAILED: 100,
    Status.DEAD_LETTERED: 100,
}


class InvalidTransition(Exception):
    pass


def can_transition(current: Status, new: Status) -> bool:
    """Re-entering the current stage is allowed (redelivery / resume after a crash)."""
    if current == new:
        return current not in TERMINAL
    return new in ALLOWED[current]


def is_terminal(status: Status | str) -> bool:
    return Status(status) in TERMINAL


def stage_index(status: Status | str) -> int:
    """Position of a status in the pipeline; QUEUED = -1, terminal = len(PIPELINE)."""
    s = Status(status)
    if s == Status.QUEUED:
        return -1
    if s in TERMINAL:
        return len(PIPELINE)
    return PIPELINE.index(s)
