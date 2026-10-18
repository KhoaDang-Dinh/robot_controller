"""Simulator adapter interface: "what world are we simulating".

An adapter owns simulator state. The runner owns recovery: it calls save_state()
at every checkpoint and load_state() on resume. An adapter must therefore put
EVERYTHING needed to continue into save_state(): simulator state, scene/asset
references, RNG state and episode cursor. For Isaac Sim, include the USD/asset
revision so resume can refuse a mismatched scene.

Bump ``state_version`` whenever the saved format changes. load_state() receives
the version the checkpoint was written with and must convert it or raise
CheckpointIncompatible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol


@dataclass
class StepResult:
    observation: Any
    reward: float
    done: bool
    info: dict = field(default_factory=dict)


class SimAdapter(Protocol):
    name: ClassVar[str]
    state_version: ClassVar[int]

    def __init__(self, *, params: dict, seed: int) -> None: ...

    def reset(self, seed: int) -> Any:
        """Start a new episode and return the first observation."""

    def observation(self) -> Any:
        """Current observation (used right after load_state)."""

    def step(self, action: Any) -> StepResult: ...

    def save_state(self) -> dict[str, bytes]: ...

    def load_state(self, files: dict[str, bytes], state_version: int) -> None: ...

    def close(self) -> None: ...
