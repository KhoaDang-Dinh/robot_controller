"""Baseline policy: always action 0. Used by recovery tests where only checkpointing matters."""
from __future__ import annotations

from robot_controller.policies import POLICIES


@POLICIES.register("noop")
class NoopPolicy:
    state_version = 1

    def __init__(self, *, params: dict, seed: int):
        if params:
            raise ValueError(f"noop policy takes no params, got {sorted(params)}")

    def act(self, observation):
        return 0

    def learn(self, observation, action, result) -> dict[str, float]:
        return {}

    def save_state(self) -> dict[str, bytes]:
        return {}

    def load_state(self, files: dict[str, bytes], state_version: int) -> None:
        # v0 = PR #2 smoke workers, which had no policy state.
        pass
