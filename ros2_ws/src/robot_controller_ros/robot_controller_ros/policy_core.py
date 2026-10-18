"""Sim-to-real control core: builds Isaac Lab observations and safe joint commands.

Pure numpy, no ROS imports, so every safety rule is unit-tested without a robot.

The observation and action conventions must match training exactly. They come from
the ``deploy/spec.json`` written by the training run (robot_controller ``isaaclab``
adapter), which records them from the live Isaac Lab env:

* observation (UR10 reach, concatenated in this order):
    joint_pos   = q - q_default            (joint_pos_rel)
    joint_vel   = dq                       (joint_vel_rel; default velocities are 0)
    pose_command = [x, y, z, qw, qx, qy, qz] target in the robot base frame
    actions     = previous raw policy output (last_action)
* action: q_target = q_default + scale * a   (JointPositionAction, use_default_offset)

Safety is layered and every layer fails closed (``Hold``):
stale or incomplete joint state, no target, non-finite policy output,
joint-limit clamping, and a per-tick step limit relative to the *measured* position.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from typing import Callable, Sequence

import numpy as np

SUPPORTED_OBSERVATIONS = ("joint_pos", "joint_vel", "pose_command", "actions")

# UR10 limits from ur_description (joint_limits.yaml): elbow ±pi, others ±2pi.
UR10_LOWER = (-2 * math.pi, -2 * math.pi, -math.pi, -2 * math.pi, -2 * math.pi, -2 * math.pi)
UR10_UPPER = (2 * math.pi, 2 * math.pi, math.pi, 2 * math.pi, 2 * math.pi, 2 * math.pi)


@dataclass(frozen=True)
class DeploySpec:
    task: str
    joint_names: tuple[str, ...]
    default_joint_pos: np.ndarray
    action_scale: float
    use_default_offset: bool
    control_period_s: float
    observation_dim: int

    @classmethod
    def from_dict(cls, raw: dict) -> DeploySpec:
        if raw.get("schema") != "robot-sim.deploy/v1":
            raise ValueError("unsupported deploy spec schema")
        joints = tuple(raw["joint_names"])
        defaults = np.asarray(raw["default_joint_pos"], dtype=np.float64)
        if len(joints) != len(defaults) or not joints:
            raise ValueError("joint_names and default_joint_pos must have the same, non-zero length")
        actions = raw["actions"]
        if len(actions) != 1 or actions[0]["type"] != "JointPositionActionCfg":
            raise ValueError("only a single JointPositionAction term is supported")
        names = tuple(term["name"] for term in raw["observations"])
        if names != SUPPORTED_OBSERVATIONS:
            raise ValueError(f"observation layout {names} is not supported; expected {SUPPORTED_OBSERVATIONS}")
        dims = [int(term["dim"]) for term in raw["observations"]]
        n = len(joints)
        if dims != [n, n, 7, n]:
            raise ValueError(f"observation dims {dims} do not match {n} joints")
        period = float(raw["control_period_s"])
        if not 0.001 <= period <= 1.0:
            raise ValueError("control_period_s out of range")
        return cls(task=raw.get("task", ""), joint_names=joints, default_joint_pos=defaults,
                   action_scale=float(actions[0]["scale"]), use_default_offset=bool(actions[0]["use_default_offset"]),
                   control_period_s=period, observation_dim=sum(dims))

    @classmethod
    def load(cls, path: str) -> DeploySpec:
        with open(path) as handle:
            return cls.from_dict(json.load(handle))


@dataclass(frozen=True)
class SafetyLimits:
    lower: np.ndarray
    upper: np.ndarray
    max_step_rad: float = 0.05          # per control tick, relative to the measured position
    joint_state_timeout_s: float = 0.1
    target_timeout_s: float = 0.0       # 0 = a target stays valid until replaced
    max_abs_action: float = 10.0        # raw policy output sanity bound
    # ros2_control mock hardware has no dynamics and may report NaN velocities.
    # Only for mock hardware: treat them as zero instead of holding.
    zero_nonfinite_velocity: bool = False

    def __post_init__(self):
        if self.max_step_rad <= 0 or self.joint_state_timeout_s <= 0:
            raise ValueError("max_step_rad and joint_state_timeout_s must be positive")
        if np.any(self.lower >= self.upper):
            raise ValueError("every joint needs lower < upper")


@dataclass
class Command:
    positions: np.ndarray
    clamped: bool
    rate_limited: bool


@dataclass
class Hold:
    reason: str


@dataclass
class _State:
    stamp: float = -math.inf
    positions: np.ndarray | None = None
    velocities: np.ndarray | None = None


def ros_pose_to_command(position: Sequence[float], orientation_xyzw: Sequence[float]) -> np.ndarray:
    """ROS quaternions are (x, y, z, w); Isaac Lab pose commands are (x, y, z, qw, qx, qy, qz)."""
    qx, qy, qz, qw = (float(v) for v in orientation_xyzw)
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    if not math.isfinite(norm) or norm < 1e-6:
        raise ValueError("invalid quaternion")
    q = np.array([qw, qx, qy, qz]) / norm
    # Same rotation, canonical sign. UR10 reach targets (roll 0, pitch pi/2, |yaw| < pi)
    # always have w >= 0 in training, so this keeps real targets in-distribution.
    if q[0] < 0:
        q = -q
    return np.concatenate([np.asarray(position, dtype=np.float64), q])


@dataclass
class PolicyController:
    spec: DeploySpec
    policy: Callable[[np.ndarray], np.ndarray]
    limits: SafetyLimits
    _joints: _State = field(default_factory=_State)
    _target: np.ndarray | None = None
    _target_stamp: float = -math.inf
    _last_action: np.ndarray | None = None

    def __post_init__(self):
        n = len(self.spec.joint_names)
        if len(self.limits.lower) != n or len(self.limits.upper) != n:
            raise ValueError("joint limits must match the spec's joints")
        self._last_action = np.zeros(n)

    # -- inputs ---------------------------------------------------------------
    def update_joint_state(self, names: Sequence[str], positions: Sequence[float],
                           velocities: Sequence[float], stamp: float) -> None:
        """Re-order by name: /joint_states order is not guaranteed to match the policy's."""
        index = {name: i for i, name in enumerate(names)}
        missing = [j for j in self.spec.joint_names if j not in index]
        if missing:
            self._joints = _State()  # incomplete state -> hold
            return
        order = [index[j] for j in self.spec.joint_names]
        pos = np.asarray(positions, dtype=np.float64)[order]
        if len(velocities) == len(names):
            vel = np.asarray(velocities, dtype=np.float64)[order]
        else:
            vel = np.full(len(order), np.nan)
        if self.limits.zero_nonfinite_velocity:
            vel = np.where(np.isfinite(vel), vel, 0.0)
        if not np.all(np.isfinite(pos)):
            self._joints = _State()
            return
        self._joints = _State(stamp=stamp, positions=pos, velocities=vel)

    def update_target(self, command: np.ndarray, stamp: float) -> None:
        command = np.asarray(command, dtype=np.float64)
        if command.shape != (7,) or not np.all(np.isfinite(command)):
            raise ValueError("target must be 7 finite values (x, y, z, qw, qx, qy, qz)")
        self._target, self._target_stamp = command, stamp

    def clear_target(self) -> None:
        self._target = None
        self._last_action = np.zeros(len(self.spec.joint_names))

    # -- control ----------------------------------------------------------------
    def observation(self) -> np.ndarray:
        q, dq = self._joints.positions, self._joints.velocities
        obs = np.concatenate([q - self.spec.default_joint_pos, dq, self._target, self._last_action])
        if obs.shape != (self.spec.observation_dim,):
            raise ValueError(f"observation has shape {obs.shape}, policy expects {self.spec.observation_dim}")
        return obs.astype(np.float32)

    def tick(self, now: float) -> Command | Hold:
        js = self._joints
        if js.positions is None:
            return Hold("no complete joint state")
        if now - js.stamp > self.limits.joint_state_timeout_s:
            return Hold("joint state is stale")
        if not np.all(np.isfinite(js.velocities)):
            return Hold("joint velocities missing or not finite")
        if self._target is None:
            return Hold("no target")
        if self.limits.target_timeout_s and now - self._target_stamp > self.limits.target_timeout_s:
            return Hold("target expired")
        action = np.asarray(self.policy(self.observation()), dtype=np.float64).reshape(-1)
        if action.shape != self._last_action.shape or not np.all(np.isfinite(action)):
            return Hold("policy output is not finite or has the wrong shape")
        if np.any(np.abs(action) > self.limits.max_abs_action):
            return Hold("policy output out of bounds")
        self._last_action = action
        base = self.spec.default_joint_pos if self.spec.use_default_offset else 0.0
        desired = base + self.spec.action_scale * action
        clamped_target = np.clip(desired, self.limits.lower, self.limits.upper)
        step = np.clip(clamped_target - js.positions, -self.limits.max_step_rad, self.limits.max_step_rad)
        command = js.positions + step
        return Command(positions=command,
                       clamped=bool(np.any(clamped_target != desired)),
                       rate_limited=bool(np.any(command != clamped_target)))


def reorder(values: np.ndarray, from_names: Sequence[str], to_names: Sequence[str]) -> list[float]:
    """Map spec-ordered values onto a controller's joint order (by name)."""
    index = {name: i for i, name in enumerate(from_names)}
    missing = [n for n in to_names if n not in index]
    if missing:
        raise ValueError(f"controller joints not in the policy: {missing}")
    return [float(values[index[n]]) for n in to_names]
