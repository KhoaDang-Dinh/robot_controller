"""A deterministic 1-D reach task: the CPU stand-in for a real simulator.

The agent's position moves by -1/0/+1 each step; reward is minus the distance to
the target; the episode ends on reaching it. It is cheap enough for CI and the
EKS recovery test, and it exercises the same save/restore path a real
simulator adapter must implement.

state_version history:
  0 - PR #2 smoke workers: progress.json {"progress": n} or {"steps": n}; no position.
  1 - state.json {"position", "target", "episode", "episode_steps"}.
"""
from __future__ import annotations

import json
import time

from robot_controller.adapters import ADAPTERS
from robot_controller.adapters.base import StepResult
from robot_controller.core.checkpoint import CheckpointIncompatible


@ADAPTERS.register("smoke")
class SmokeAdapter:
    state_version = 1

    def __init__(self, *, params: dict, seed: int):
        unknown = set(params) - {"target_position", "step_seconds", "max_episode_steps"}
        if unknown:
            raise ValueError(f"smoke adapter: unknown params {sorted(unknown)}")
        self.target = int(params.get("target_position", 10))
        self.step_seconds = float(params.get("step_seconds", 1.0))
        self.max_episode_steps = int(params.get("max_episode_steps", 100))
        if self.step_seconds < 0 or self.max_episode_steps < 1:
            raise ValueError("smoke adapter: step_seconds >= 0 and max_episode_steps >= 1 required")
        self.seed = seed
        self.position = 0
        self.episode = 0
        self.episode_steps = 0

    def reset(self, seed: int) -> dict:
        self.seed = seed
        self.position = 0
        self.episode_steps = 0
        return self.observation()

    def observation(self) -> dict:
        return {"position": self.position, "target": self.target}

    def step(self, action) -> StepResult:
        if action not in (-1, 0, 1):
            raise ValueError(f"smoke adapter: action must be -1, 0 or 1, got {action!r}")
        if self.step_seconds:
            time.sleep(self.step_seconds)
        self.position += action
        self.episode_steps += 1
        reached = self.position == self.target
        done = reached or self.episode_steps >= self.max_episode_steps
        result = StepResult(self.observation(), -float(abs(self.target - self.position)), done,
                            {"reached": reached, "episode": self.episode})
        if done:
            self.episode += 1
            self.reset(self.seed + 1)
        return result

    def save_state(self) -> dict[str, bytes]:
        return {"state.json": json.dumps({
            "position": self.position, "target": self.target,
            "episode": self.episode, "episode_steps": self.episode_steps, "seed": self.seed,
        }, sort_keys=True).encode()}

    def load_state(self, files: dict[str, bytes], state_version: int) -> None:
        if state_version == 0:
            # PR #2 workers only stored a step counter; the runner restores that.
            if "progress.json" not in files:
                raise CheckpointIncompatible("smoke v0 checkpoint without progress.json")
            self.reset(self.seed)
            return
        if state_version != 1:
            raise CheckpointIncompatible(f"smoke adapter cannot read state_version {state_version}")
        state = json.loads(files["state.json"])
        if state["target"] != self.target:
            raise CheckpointIncompatible("checkpoint was written for a different target_position")
        self.position, self.episode = state["position"], state["episode"]
        self.episode_steps, self.seed = state["episode_steps"], state["seed"]

    def close(self) -> None:
        pass
