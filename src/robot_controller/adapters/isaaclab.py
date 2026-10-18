"""Isaac Lab adapter: a GPU-vectorized Isaac Lab task (thousands of parallel envs).

Pinned stack (see containers/Dockerfile.isaaclab): Isaac Lab v2.3.2 on Isaac Sim 5.1.0,
Python 3.11. Isaac Lab / Isaac Sim are imported lazily, only inside the training
image, so ``robot-run validate`` and CI never need a GPU or the simulator.

This adapter is driven by a *vectorized learner* policy (``rsl_rl_ppo``): the
learner pulls ``gym_env()`` and runs whole PPO iterations; per-step ``step()``
is intentionally unsupported.

Checkpointing: for on-policy RL the simulator state is not worth saving: after a
resume the parallel environments are simply reset, losing at most one partial
rollout. ``save_state`` therefore records only what must match on resume (task,
env count, Isaac Lab / Isaac Sim versions); the learner state lives in the policy.

``artifacts()`` writes ``deploy/spec.json``: joint names, default joint positions,
action scaling, control period and observation layout, read from the *live* env.
The ROS 2 node (ros2_ws/src/robot_controller_ros) builds observations and
commands from that file, so sim and robot cannot silently disagree.
"""
from __future__ import annotations

import json
import re
from typing import Any

from robot_controller.adapters import ADAPTERS
from robot_controller.core.checkpoint import CheckpointIncompatible

DEVICE = re.compile(r"^(cpu|cuda(:\d+)?)$")
PARAMS = {"task", "num_envs", "device", "enable_cameras", "env_overrides"}


def apply_overrides(cfg: Any, overrides: dict[str, Any]) -> None:
    """Set dotted attributes on an Isaac Lab configclass, e.g. {"scene.num_envs": 64}."""
    for dotted, value in overrides.items():
        target = cfg
        *parents, leaf = dotted.split(".")
        for name in parents:
            target = getattr(target, name)
        if not hasattr(target, leaf):
            raise ValueError(f"unknown config field {dotted!r}")
        setattr(target, leaf, value)


@ADAPTERS.register("isaaclab")
class IsaacLabAdapter:
    state_version = 1

    def __init__(self, *, params: dict, seed: int):
        unknown = set(params) - PARAMS
        if unknown:
            raise ValueError(f"isaaclab adapter: unknown params {sorted(unknown)}")
        self.task = params.get("task")
        if not isinstance(self.task, str) or not self.task.startswith("Isaac-"):
            raise ValueError("isaaclab adapter: params.task must be a registered task id such as Isaac-Reach-UR10-v0")
        self.num_envs = params.get("num_envs", 4096)
        if not isinstance(self.num_envs, int) or self.num_envs < 1:
            raise ValueError("isaaclab adapter: num_envs must be a positive integer")
        self.device = params.get("device", "cuda:0")
        if not isinstance(self.device, str) or not DEVICE.match(self.device):
            raise ValueError("isaaclab adapter: device must be cpu, cuda or cuda:N")
        self.enable_cameras = bool(params.get("enable_cameras", False))
        self.env_overrides = params.get("env_overrides", {})
        if not isinstance(self.env_overrides, dict) or not all(isinstance(k, str) for k in self.env_overrides):
            raise ValueError("isaaclab adapter: env_overrides must map dotted field names to values")
        self.seed = seed
        self._app = None
        self._env = None
        self.versions: dict[str, str] = {}

    # -- lifecycle -------------------------------------------------------------
    def _launch(self):  # pragma: no cover - needs Isaac Sim; replaced by a fake in tests
        """Start the headless Kit app, then build the gymnasium env. Order matters:
        Isaac Lab modules can only be imported after the SimulationApp exists."""
        from isaaclab.app import AppLauncher

        app = AppLauncher(headless=True, device=self.device, enable_cameras=self.enable_cameras).app
        import importlib.metadata as metadata

        import gymnasium as gym
        import isaaclab_tasks  # noqa: F401  (registers Isaac-* tasks)
        from isaaclab_tasks.utils import parse_env_cfg

        env_cfg = parse_env_cfg(self.task, device=self.device, num_envs=self.num_envs)
        env_cfg.seed = self.seed
        apply_overrides(env_cfg, self.env_overrides)
        env = gym.make(self.task, cfg=env_cfg)
        versions = {}
        for package in ("isaaclab", "isaacsim", "rsl-rl-lib"):
            try:
                versions[package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                versions[package] = "unknown"
        return app, env, versions

    def _ensure(self):
        if self._env is None:
            self._app, self._env, self.versions = self._launch()
        return self._env

    def gym_env(self):
        """The gymnasium env, for vectorized learners (rsl_rl wraps it itself)."""
        return self._ensure()

    def reset(self, seed: int):
        self.seed = seed
        self._ensure()
        return None  # the learner resets the vectorized env itself

    def observation(self):
        self._ensure()
        return None

    def step(self, action):
        raise NotImplementedError("isaaclab adapter is driven by a vectorized learner (e.g. rsl_rl_ppo), "
                                  "not by a per-step policy")

    # -- checkpoint ------------------------------------------------------------
    def save_state(self) -> dict[str, bytes]:
        return {"adapter.json": json.dumps({
            "task": self.task, "num_envs": self.num_envs, "versions": self.versions,
        }, sort_keys=True).encode()}

    def load_state(self, files: dict[str, bytes], state_version: int) -> None:
        if state_version != 1:
            raise CheckpointIncompatible(f"isaaclab adapter cannot read state_version {state_version}")
        stored = json.loads(files["adapter.json"])
        if stored["task"] != self.task:
            raise CheckpointIncompatible(f"checkpoint is for task {stored['task']!r}, experiment uses {self.task!r}")
        # Different env counts change the PPO batch shape; refuse rather than resume a different experiment.
        if stored["num_envs"] != self.num_envs:
            raise CheckpointIncompatible(
                f"checkpoint used num_envs={stored['num_envs']}, experiment uses {self.num_envs}")
        self._ensure()

    # -- deployment spec --------------------------------------------------------
    def deploy_spec(self) -> dict:
        env = self._ensure().unwrapped
        robot = env.scene["robot"]
        actions = []
        for name in env.action_manager.active_terms:
            cfg = env.action_manager.get_term(name).cfg
            actions.append({
                "name": name, "type": type(cfg).__name__,
                "joint_names": list(getattr(cfg, "joint_names", [])),
                "scale": getattr(cfg, "scale", 1.0),
                "use_default_offset": getattr(cfg, "use_default_offset", False),
            })
        obs_terms = env.observation_manager.active_terms["policy"]
        obs_dims = env.observation_manager.group_obs_term_dim["policy"]
        return {
            "schema": "robot-sim.deploy/v1",
            "task": self.task,
            "versions": self.versions,
            "control_period_s": float(env.step_dt),
            "joint_names": list(robot.joint_names),
            "default_joint_pos": [float(x) for x in robot.data.default_joint_pos[0].tolist()],
            "actions": actions,
            "observations": [{"name": n, "dim": int(d[0])} for n, d in zip(obs_terms, obs_dims, strict=True)],
        }

    def artifacts(self) -> dict[str, bytes]:
        if self._env is None:
            return {}
        return {"deploy/spec.json": json.dumps(self.deploy_spec(), indent=2, default=str).encode()}

    def close(self) -> None:
        if self._env is not None:
            self._env.close()
            self._env = None
        if self._app is not None:
            self._app.close()
            self._app = None
