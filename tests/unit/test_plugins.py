import json

import pytest

from robot_controller.adapters import ADAPTERS
from robot_controller.core.checkpoint import CheckpointIncompatible
from robot_controller.policies import POLICIES


def test_registry_lists_and_rejects_unknown():
    assert "smoke" in ADAPTERS.names()
    assert {"noop", "epsilon_greedy"} <= set(POLICIES.names())
    with pytest.raises(KeyError, match="registered"):
        ADAPTERS.create("nope", {}, seed=0)


def test_smoke_adapter_reaches_target_and_round_trips_state():
    adapter = ADAPTERS.create("smoke", {"target_position": 2, "step_seconds": 0}, seed=1)
    adapter.reset(1)
    assert adapter.step(1).done is False
    result = adapter.step(1)
    assert result.done and result.info["reached"] and result.reward == 0
    adapter.step(1)
    clone = ADAPTERS.create("smoke", {"target_position": 2, "step_seconds": 0}, seed=1)
    clone.load_state(adapter.save_state(), adapter.state_version)
    assert clone.observation() == adapter.observation() and clone.episode == 1
    with pytest.raises(CheckpointIncompatible):
        other = ADAPTERS.create("smoke", {"target_position": 5, "step_seconds": 0}, seed=1)
        other.load_state(adapter.save_state(), 1)
    with pytest.raises(ValueError):
        ADAPTERS.create("smoke", {"typo": 1}, seed=0)


def test_smoke_adapter_reads_pr2_checkpoint_format():
    adapter = ADAPTERS.create("smoke", {"step_seconds": 0}, seed=0)
    adapter.load_state({"progress.json": json.dumps({"progress": 7}).encode()}, 0)
    assert adapter.observation()["position"] == 0
    with pytest.raises(CheckpointIncompatible):
        adapter.load_state({}, 0)
    with pytest.raises(CheckpointIncompatible):
        adapter.load_state({}, 99)


def test_epsilon_greedy_is_deterministic_across_save_and_restore():
    params = {"epsilon": 0.5, "decay": 0.9}
    adapter = ADAPTERS.create("smoke", {"step_seconds": 0, "target_position": 3}, seed=4)
    policy = POLICIES.create("epsilon_greedy", params, seed=4)
    obs = adapter.reset(4)
    for _ in range(20):
        action = policy.act(obs)
        result = adapter.step(action)
        metrics = policy.learn(obs, action, result)
        obs = result.observation
    assert set(metrics) == {"mean_reward", "epsilon"}
    restored = POLICIES.create("epsilon_greedy", params, seed=999)
    restored.load_state(policy.save_state(), policy.state_version)
    assert [restored.act(obs) for _ in range(30)] == [policy.act(obs) for _ in range(30)]
