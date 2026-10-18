"""Sim-to-real control core: observation layout, action mapping and every safety layer."""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from robot_controller_ros.policy_core import (
    UR10_LOWER, UR10_UPPER, Command, DeploySpec, Hold, PolicyController, SafetyLimits, reorder,
    ros_pose_to_command,
)

SPEC_FILE = Path(__file__).resolve().parents[1] / "config" / "ur10_reach_spec.json"
JOINTS = ["shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint", "wrist_1_joint", "wrist_2_joint", "wrist_3_joint"]
DEFAULTS = np.array([0.0, -1.712, 1.712, 0.0, 0.0, 0.0])


def spec():
    return DeploySpec.load(str(SPEC_FILE))


def limits(**kw):
    return SafetyLimits(lower=np.array(UR10_LOWER), upper=np.array(UR10_UPPER), **kw)


def controller(policy, **kw):
    c = PolicyController(spec(), policy, limits(**kw))
    c.update_joint_state(JOINTS, DEFAULTS, np.zeros(6), stamp=0.0)
    c.update_target(ros_pose_to_command((0.5, 0.0, 0.3), (0, 0.7071068, 0, 0.7071068)), stamp=0.0)
    return c


def test_bundled_spec_matches_isaaclab_ur10_reach():
    s = spec()
    assert s.joint_names == tuple(JOINTS)
    assert s.observation_dim == 25 and s.action_scale == 0.5 and s.use_default_offset
    assert s.control_period_s == pytest.approx(1 / 30)


def test_observation_layout_matches_training():
    seen = {}

    def policy(obs):
        seen["obs"] = obs
        return np.full(6, 0.1)
    c = controller(policy)
    c.update_joint_state(JOINTS, DEFAULTS + 0.01, np.full(6, 0.2), stamp=0.0)
    c.tick(0.01)
    obs = seen["obs"]
    assert obs.dtype == np.float32 and obs.shape == (25,)
    np.testing.assert_allclose(obs[0:6], 0.01, atol=1e-6)       # joint_pos_rel
    np.testing.assert_allclose(obs[6:12], 0.2, atol=1e-6)       # joint_vel_rel
    np.testing.assert_allclose(obs[12:15], [0.5, 0.0, 0.3], atol=1e-6)
    np.testing.assert_allclose(obs[15:19], [0.7071068, 0, 0.7071068, 0], atol=1e-6)  # w first
    np.testing.assert_allclose(obs[19:25], 0.0)                 # no previous action yet
    c.tick(0.02)
    np.testing.assert_allclose(seen["obs"][19:25], 0.1, atol=1e-6)  # last raw action fed back


def test_action_maps_to_default_offset_plus_scale_within_step_limit():
    c = controller(lambda obs: np.full(6, 0.02), max_step_rad=1.0)
    result = c.tick(0.01)
    assert isinstance(result, Command) and not result.clamped and not result.rate_limited
    np.testing.assert_allclose(result.positions, DEFAULTS + 0.01)


def test_step_limit_is_relative_to_measured_position():
    c = controller(lambda obs: np.full(6, 2.0), max_step_rad=0.05)
    result = c.tick(0.01)
    assert result.rate_limited
    np.testing.assert_allclose(result.positions, DEFAULTS + 0.05)


def test_joint_limits_clamp_targets():
    c = controller(lambda obs: np.array([0, 0, 9.0, 0, 0, 0]), max_step_rad=100.0)
    result = c.tick(0.01)
    assert result.clamped and result.positions[2] == pytest.approx(math.pi)


@pytest.mark.parametrize("policy, now, reason", [
    (lambda obs: np.full(6, np.nan), 0.01, "not finite"),
    (lambda obs: np.ones(5), 0.01, "wrong shape"),
    (lambda obs: np.full(6, 50.0), 0.01, "out of bounds"),
    (lambda obs: np.zeros(6), 0.5, "stale"),
])
def test_unsafe_inputs_hold(policy, now, reason):
    result = controller(policy).tick(now)
    assert isinstance(result, Hold) and reason in result.reason


def test_holds_without_target_or_with_partial_joint_state():
    c = controller(lambda obs: np.zeros(6))
    c.clear_target()
    assert isinstance(c.tick(0.01), Hold)
    c = controller(lambda obs: np.zeros(6))
    c.update_joint_state(JOINTS[:5], DEFAULTS[:5], np.zeros(5), stamp=0.0)
    assert "joint state" in c.tick(0.01).reason


def test_joint_states_are_reordered_by_name():
    seen = {}
    c = controller(lambda obs: seen.setdefault("obs", obs) * 0)
    shuffled = list(reversed(JOINTS))
    c.update_joint_state(shuffled, list(reversed(DEFAULTS + np.arange(6) * 0.01)), [0.0] * 6, stamp=0.0)
    c.tick(0.01)
    np.testing.assert_allclose(seen["obs"][0:6], np.arange(6) * 0.01, atol=1e-6)


def test_target_expiry():
    c = controller(lambda obs: np.zeros(6), target_timeout_s=0.2)
    c.update_joint_state(JOINTS, DEFAULTS, np.zeros(6), stamp=0.5)
    assert "expired" in c.tick(0.55).reason


def test_quaternion_conversion_and_validation():
    cmd = ros_pose_to_command((1, 2, 3), (0, 0, 0, -2))  # unnormalised, w < 0
    np.testing.assert_allclose(cmd, [1, 2, 3, 1, 0, 0, 0])
    with pytest.raises(ValueError):
        ros_pose_to_command((0, 0, 0), (0, 0, 0, 0))


def test_reorder_to_controller_joints():
    values = np.arange(6.0)
    assert reorder(values, JOINTS, list(reversed(JOINTS))) == [5, 4, 3, 2, 1, 0]
    with pytest.raises(ValueError):
        reorder(values, JOINTS, ["gripper"])


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(schema="x"), "schema"),
    (lambda d: d["observations"].append({"name": "camera", "dim": 3}), "not supported"),
    (lambda d: d["observations"][2].update(dim=4), "dims"),
    (lambda d: d["actions"][0].update(type="DifferentialIKControllerActionCfg"), "JointPositionAction"),
    (lambda d: d.update(control_period_s=5), "control_period"),
])
def test_spec_rejects_layouts_the_node_cannot_reproduce(mutate, message):
    raw = json.loads(SPEC_FILE.read_text())
    mutate(raw)
    with pytest.raises(ValueError, match=message):
        DeploySpec.from_dict(raw)


def test_limits_validation():
    with pytest.raises(ValueError):
        SafetyLimits(lower=np.zeros(6), upper=np.zeros(6))


def test_missing_or_nan_velocity_holds_on_real_hardware_but_not_on_mock():
    c = controller(lambda obs: np.zeros(6))
    c.update_joint_state(JOINTS, DEFAULTS, [float("nan")] * 6, stamp=0.0)
    assert "velocities" in c.tick(0.01).reason
    c.update_joint_state(JOINTS, DEFAULTS, [], stamp=0.0)
    assert "velocities" in c.tick(0.01).reason
    mock = controller(lambda obs: np.zeros(6), zero_nonfinite_velocity=True)
    mock.update_joint_state(JOINTS, DEFAULTS, [float("nan")] * 6, stamp=0.0)
    assert isinstance(mock.tick(0.01), Command)


def test_nonfinite_positions_hold():
    c = controller(lambda obs: np.zeros(6))
    c.update_joint_state(JOINTS, [float("nan")] * 6, np.zeros(6), stamp=0.0)
    assert "joint state" in c.tick(0.01).reason
