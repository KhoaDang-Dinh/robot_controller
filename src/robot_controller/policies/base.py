"""Policy interface: "what decides actions and how it learns".

Each new idea is a new module in this package plus an experiment file that
selects it. The runner handles fencing, checkpoints, Spot/SIGTERM and barriers,
so a policy only has to act, learn and serialise itself.

save_state() must include everything needed to continue training identically:
weights, optimizer state, RNG state, replay-buffer cursor. Bump ``state_version``
when that format changes. load_state() receives the stored version.
"""
from __future__ import annotations

from typing import Any, ClassVar, Protocol

from robot_controller.adapters.base import StepResult


class Policy(Protocol):
    name: ClassVar[str]
    state_version: ClassVar[int]

    def __init__(self, *, params: dict, seed: int) -> None: ...

    def act(self, observation: Any) -> Any: ...

    def learn(self, observation: Any, action: Any, result: StepResult) -> dict[str, float]:
        """Update from one transition; return metrics to report (may be empty)."""

    def save_state(self) -> dict[str, bytes]: ...

    def load_state(self, files: dict[str, bytes], state_version: int) -> None: ...
