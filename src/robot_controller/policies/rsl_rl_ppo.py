"""PPO via rsl_rl (Isaac Lab's default RL library), as a vectorized learner.

One runner step = ``iterations_per_step`` PPO iterations (each: ``num_steps_per_env``
steps of every parallel env, then the update). The core runner checkpoints between
runner steps, so recovery granularity is a few iterations, not a whole run.

Checkpointed state (``save_state``), as separate files:
  learner/model.pt   rsl_rl's own checkpoint (actor-critic incl. observation
                     normalizers, optimizer state, iteration), loaded by rsl_rl
  learner/rng.pt     torch / CUDA RNG tensors (loaded with weights_only=True)
  learner/meta.json  our iteration counter
Rollout buffers are not saved: PPO discards them every iteration anyway. Checkpoints
are trusted input: only fenced writers of this platform write them, into the
project's own bucket (rsl_rl's loader unpickles its file).

Artifacts at the end of a run: ``policy/policy.onnx`` and ``policy/policy.pt``
(TorchScript), exported with Isaac Lab's exporter, normalizer included. The ROS 2
deployment node runs the ONNX file.

The agent configuration starts from the task's registered ``rsl_rl_cfg_entry_point``
(e.g. UR10ReachPPORunnerCfg) and applies ``overrides`` with dotted keys::

    policy:
      name: rsl_rl_ppo
      params:
        iterations_per_step: 10
        overrides: {algorithm.learning_rate: 3.0e-4, policy.actor_hidden_dims: [128, 128]}
"""
from __future__ import annotations

import io
import json
import os
import tempfile
from robot_controller.adapters.isaaclab import apply_overrides
from robot_controller.core.checkpoint import CheckpointIncompatible
from robot_controller.policies import POLICIES

PARAMS = {"iterations_per_step", "overrides", "export"}


@POLICIES.register("rsl_rl_ppo")
class RslRlPpoPolicy:
    state_version = 1

    def __init__(self, *, params: dict, seed: int):
        unknown = set(params) - PARAMS
        if unknown:
            raise ValueError(f"rsl_rl_ppo: unknown params {sorted(unknown)}")
        self.iterations_per_step = params.get("iterations_per_step", 10)
        if not isinstance(self.iterations_per_step, int) or self.iterations_per_step < 1:
            raise ValueError("rsl_rl_ppo: iterations_per_step must be a positive integer")
        self.overrides = params.get("overrides", {})
        if not isinstance(self.overrides, dict) or not all(isinstance(k, str) for k in self.overrides):
            raise ValueError("rsl_rl_ppo: overrides must map dotted field names to values")
        self.export = bool(params.get("export", True))
        self.seed = seed
        self.iteration = 0
        self._runner = None
        self._pending_state: dict[str, bytes] | None = None
        self._last_metrics: dict[str, float] = {}
        self._torch = None
        self._first = True  # randomise initial episode lengths on the first iteration only

    # -- per-step interface is not used by vectorized learners -----------------
    def act(self, observation):
        raise NotImplementedError("rsl_rl_ppo is a vectorized learner; the runner calls train_step()")

    def learn(self, observation, action, result):
        raise NotImplementedError("rsl_rl_ppo is a vectorized learner; the runner calls train_step()")

    # -- construction (needs Isaac Lab / rsl_rl; replaced by fakes in tests) ---
    def _build(self, adapter):  # pragma: no cover - requires the training image
        import torch
        from isaaclab_rl.rsl_rl import RslRlVecEnvWrapper
        from isaaclab_tasks.utils import load_cfg_from_registry
        from rsl_rl.runners import OnPolicyRunner

        agent_cfg = load_cfg_from_registry(adapter.task, "rsl_rl_cfg_entry_point")
        agent_cfg.seed = self.seed
        agent_cfg.device = adapter.device
        apply_overrides(agent_cfg, self.overrides)
        vec_env = RslRlVecEnvWrapper(adapter.gym_env(), clip_actions=agent_cfg.clip_actions)
        log_dir = os.path.join(tempfile.gettempdir(), "rsl_rl")
        runner = OnPolicyRunner(vec_env, agent_cfg.to_dict(), log_dir=log_dir, device=agent_cfg.device)
        return torch, runner

    def _ensure(self, adapter):
        if self._runner is None:
            self._torch, self._runner = self._build(adapter)
            self._capture_metrics(self._runner)
            if self._pending_state is not None:
                self._restore(self._pending_state)
                self._pending_state = None
                self._first = False  # resumed: envs restart anyway, keep rsl_rl's default
        return self._runner

    def _capture_metrics(self, runner) -> None:
        """Keep the latest scalars rsl_rl logs, without depending on TensorBoard files."""
        original = runner.log

        def log(locs, *args, **kwargs):
            metrics = {}
            for key, value in (locs.get("loss_dict") or {}).items():
                metrics[f"loss_{key}"] = float(value)
            rewards, lengths = locs.get("rewbuffer"), locs.get("lenbuffer")
            if rewards:
                metrics["mean_reward"] = float(sum(rewards) / len(rewards))
            if lengths:
                metrics["mean_episode_length"] = float(sum(lengths) / len(lengths))
            metrics["learning_rate"] = float(getattr(runner.alg, "learning_rate", 0.0))
            steps = runner.num_steps_per_env * runner.env.num_envs
            elapsed = float(locs.get("collection_time", 0)) + float(locs.get("learn_time", 0))
            if elapsed > 0:
                metrics["fps"] = round(steps / elapsed, 1)
            self._last_metrics = {k: round(v, 6) for k, v in metrics.items()}
            return original(locs, *args, **kwargs)

        runner.log = log

    # -- training ---------------------------------------------------------------
    def train_step(self, adapter) -> dict[str, float]:
        runner = self._ensure(adapter)
        # rsl_rl starts counting at current_learning_iteration; keep it equal to ours.
        runner.current_learning_iteration = self.iteration
        runner.learn(num_learning_iterations=self.iterations_per_step,
                     init_at_random_ep_len=self._first)
        self._first = False
        self.iteration += self.iterations_per_step
        return {"iteration": self.iteration, **self._last_metrics}

    # -- checkpoint -------------------------------------------------------------
    def _serialize(self) -> dict[str, bytes]:
        runner, torch = self._runner, self._torch
        runner.current_learning_iteration = self.iteration
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "model.pt")
            runner.save(path, infos={"robot_controller_iteration": self.iteration})
            with open(path, "rb") as handle:
                model = handle.read()
        rng = {"torch": torch.get_rng_state()}
        if torch.cuda.is_available():
            rng["cuda"] = torch.cuda.get_rng_state_all()
        buffer = io.BytesIO()
        torch.save(rng, buffer)
        return {"learner/model.pt": model, "learner/rng.pt": buffer.getvalue(),
                "learner/meta.json": json.dumps({"iteration": self.iteration}).encode()}

    def _restore(self, files: dict[str, bytes]) -> None:
        torch = self._torch
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "model.pt")
            with open(path, "wb") as handle:
                handle.write(files["learner/model.pt"])
            self._runner.load(path, load_optimizer=True)
        rng = torch.load(io.BytesIO(files["learner/rng.pt"]), map_location="cpu", weights_only=True)
        torch.set_rng_state(rng["torch"])
        if "cuda" in rng and torch.cuda.is_available():
            torch.cuda.set_rng_state_all(rng["cuda"])
        self.iteration = int(json.loads(files["learner/meta.json"])["iteration"])

    def save_state(self) -> dict[str, bytes]:
        if self._runner is None:
            return dict(self._pending_state or {})
        return self._serialize()

    def load_state(self, files: dict[str, bytes], state_version: int) -> None:
        required = {"learner/model.pt", "learner/rng.pt", "learner/meta.json"}
        if state_version != 1 or not required <= set(files):
            raise CheckpointIncompatible(f"rsl_rl_ppo cannot read state_version {state_version} "
                                         "or a checkpoint without learner state")
        # The runner (and CUDA) only exist after the simulator starts; restore lazily.
        self._pending_state = {name: files[name] for name in required}
        if self._runner is not None:
            self._restore(self._pending_state)
            self._pending_state = None

    # -- export -----------------------------------------------------------------
    def artifacts(self) -> dict[str, bytes]:  # pragma: no cover - requires the training image
        if not self.export or self._runner is None:
            return {}
        from isaaclab_rl.rsl_rl import export_policy_as_jit, export_policy_as_onnx

        policy_nn = self._runner.alg.policy
        normalizer = getattr(policy_nn, "actor_obs_normalizer", None)
        files: dict[str, bytes] = {}
        with tempfile.TemporaryDirectory() as tmp:
            export_policy_as_onnx(policy_nn, path=tmp, normalizer=normalizer, filename="policy.onnx")
            export_policy_as_jit(policy_nn, normalizer=normalizer, path=tmp, filename="policy.pt")
            for name in ("policy.onnx", "policy.pt"):
                with open(os.path.join(tmp, name), "rb") as handle:
                    files[f"policy/{name}"] = handle.read()
        return files

