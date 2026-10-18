"""Example learning policy: epsilon-greedy toward the observed target.

It is deliberately small. It shows the parts every real policy needs: seeded
RNG whose state is checkpointed, a learning signal (running reward average and
an epsilon schedule), and metrics returned to telemetry. Copy this module to
start a new idea.
"""
from __future__ import annotations

import json
import random

from robot_controller.core.checkpoint import CheckpointIncompatible
from robot_controller.policies import POLICIES


@POLICIES.register("epsilon_greedy")
class EpsilonGreedyPolicy:
    state_version = 1

    def __init__(self, *, params: dict, seed: int):
        unknown = set(params) - {"epsilon", "epsilon_min", "decay"}
        if unknown:
            raise ValueError(f"epsilon_greedy: unknown params {sorted(unknown)}")
        self.epsilon = float(params.get("epsilon", 0.5))
        self.epsilon_min = float(params.get("epsilon_min", 0.05))
        self.decay = float(params.get("decay", 0.99))
        if not (0 <= self.epsilon_min <= self.epsilon <= 1 and 0 < self.decay <= 1):
            raise ValueError("epsilon_greedy: need 0 <= epsilon_min <= epsilon <= 1 and 0 < decay <= 1")
        self.rng = random.Random(seed)  # noqa: S311 - exploration, not security
        self.mean_reward = 0.0
        self.updates = 0

    def act(self, observation):
        if self.rng.random() < self.epsilon:
            return self.rng.choice((-1, 0, 1))
        gap = observation["target"] - observation["position"]
        return (gap > 0) - (gap < 0)

    def learn(self, observation, action, result) -> dict[str, float]:
        self.updates += 1
        self.mean_reward += (result.reward - self.mean_reward) / min(self.updates, 100)
        if result.done:
            self.epsilon = max(self.epsilon_min, self.epsilon * self.decay)
        return {"mean_reward": round(self.mean_reward, 4), "epsilon": round(self.epsilon, 4)}

    def save_state(self) -> dict[str, bytes]:
        version, internal, gauss = self.rng.getstate()
        return {"state.json": json.dumps({
            "epsilon": self.epsilon, "mean_reward": self.mean_reward, "updates": self.updates,
            "rng": [version, list(internal), gauss],
        }).encode()}

    def load_state(self, files: dict[str, bytes], state_version: int) -> None:
        if state_version != 1:
            raise CheckpointIncompatible(f"epsilon_greedy cannot read state_version {state_version}")
        state = json.loads(files["state.json"])
        self.epsilon, self.mean_reward, self.updates = state["epsilon"], state["mean_reward"], state["updates"]
        version, internal, gauss = state["rng"]
        self.rng.setstate((version, tuple(internal), gauss))
