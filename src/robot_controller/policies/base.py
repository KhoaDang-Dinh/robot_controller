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


class VectorizedLearner(Protocol):
    """A learner that drives a vectorized adapter itself (e.g. PPO over 4096 Isaac Lab envs).

    The runner calls ``train_step(adapter)`` instead of act/step/learn; one call is one
    unit of progress (``target_steps`` counts these). Recovery stays in the runner.
    """
    name: ClassVar[str]
    state_version: ClassVar[int]

    def train_step(self, adapter: Any) -> dict[str, float]: ...

    def save_state(self) -> dict[str, bytes]: ...

    def load_state(self, files: dict[str, bytes], state_version: int) -> None: ...

    def artifacts(self) -> dict[str, bytes]:
        """Optional: exported models uploaded with the results (e.g. policy/policy.onnx)."""
