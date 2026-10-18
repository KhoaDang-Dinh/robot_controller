"""Isaac Lab adapter + rsl_rl PPO learner, driven by the real runner with fakes.

The fakes mimic the exact Isaac Lab v2.3.2 / rsl_rl 3.1 surfaces the plug-ins touch
(AppLauncher env, managers, OnPolicyRunner.learn/log/save/load), so CI checks the
recovery wiring without a GPU. The real stack is exercised on EKS (docs/ISAACLAB.md).
"""
import json
import pickle

import pytest

from robot_controller.adapters import ADAPTERS
from robot_controller.adapters.isaaclab import IsaacLabAdapter, apply_overrides
from robot_controller.core.experiment import parse
from robot_controller.core.runner import COMPLETED, INCOMPATIBLE, Runner, RuntimeEnv
from robot_controller.policies import POLICIES
from robot_controller.policies.rsl_rl_ppo import RslRlPpoPolicy
from tests.conftest import seed_external

JOINTS = ["shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint", "wrist_1_joint", "wrist_2_joint", "wrist_3_joint"]
DEFAULTS = [0.0, -1.712, 1.712, 0.0, 0.0, 0.0]


class _Row(list):
    def tolist(self):
        return list(self)


class FakeEnv:
    """Shape of an Isaac Lab ManagerBasedRLEnv as seen through gym.make(...).unwrapped."""
    closed = False

    def __init__(self):
        robot = type("Robot", (), {"joint_names": JOINTS,
                                   "data": type("D", (), {"default_joint_pos": [_Row(DEFAULTS)]})()})()
        action_cfg = type("JointPositionActionCfg", (), {"joint_names": [".*"], "scale": 0.5,
                                                          "use_default_offset": True})()
        self.unwrapped = self
        self.scene = {"robot": robot}
        self.step_dt = 1 / 30
        self.action_manager = type("AM", (), {
            "active_terms": ["arm_action"],
            "get_term": lambda self, name: type("T", (), {"cfg": action_cfg})()})()
        self.observation_manager = type("OM", (), {
            "active_terms": {"policy": ["joint_pos", "joint_vel", "pose_command", "actions"]},
            "group_obs_term_dim": {"policy": [(6,), (6,), (7,), (6,)]}})()

    def close(self):
        FakeEnv.closed = True


class FakeApp:
    def close(self):
        pass


class FakeRunner:
    """OnPolicyRunner surface: learn() logs once per iteration; save/load round-trip weights."""
    loads = 0

    def __init__(self):
        self.num_steps_per_env = 24
        self.env = type("E", (), {"num_envs": 64})()
        self.alg = type("A", (), {"learning_rate": 1e-3})()
        self.current_learning_iteration = 0
        self.weights = 0.0
        self.init_flags = []

    def log(self, locs):
        pass

    def learn(self, num_learning_iterations, init_at_random_ep_len=False):
        self.init_flags.append(init_at_random_ep_len)
        start = self.current_learning_iteration
        for it in range(start, start + num_learning_iterations):
            self.weights += 1.0
            self.log({"it": it, "loss_dict": {"value_function": 0.5, "surrogate": -0.1},
                      "rewbuffer": [-1.0, -0.5], "lenbuffer": [360.0], "collection_time": 0.2, "learn_time": 0.1})
            self.current_learning_iteration = it

    def save(self, path, infos=None):
        with open(path, "w") as handle:
            json.dump({"weights": self.weights, "iter": self.current_learning_iteration, "infos": infos}, handle)

    def load(self, path, load_optimizer=True):
        FakeRunner.loads += 1
        with open(path) as handle:
            self.weights = json.load(handle)["weights"]


class FakeTorch:
    class cuda:  # noqa: N801 - mirrors torch.cuda
        @staticmethod
        def is_available():
            return False

    @staticmethod
    def get_rng_state():
        return b"rng"

    @staticmethod
    def set_rng_state(state):
        assert state == b"rng"

    @staticmethod
    def save(obj, buffer):
        pickle.dump(obj, buffer)

    @staticmethod
    def load(buffer, map_location=None, weights_only=False):
        assert weights_only, "RNG state must be loaded with weights_only=True"
        return pickle.load(buffer)  # noqa: S301 - test fixture data only


@pytest.fixture()
def fake_isaaclab(monkeypatch):
    runners = []

    def build(self, adapter):
        adapter.gym_env()
        runners.append(FakeRunner())
        return FakeTorch, runners[-1]
    monkeypatch.setattr(IsaacLabAdapter, "_launch",
                        lambda self: (FakeApp(), FakeEnv(), {"isaaclab": "2.3.2", "isaacsim": "5.1.0"}))
    monkeypatch.setattr(RslRlPpoPolicy, "_build", build)
    monkeypatch.setattr(RslRlPpoPolicy, "artifacts", lambda self: {"policy/policy.onnx": b"onnx-bytes"})
    FakeRunner.loads = 0
    return runners


def experiment(target=3, num_envs=64, extra=""):
    return parse(f"""schema_version: 1
name: ur10-unit
adapter: {{name: isaaclab, params: {{task: Isaac-Reach-UR10-v0, num_envs: {num_envs}}}}}
policy: {{name: rsl_rl_ppo, params: {{iterations_per_step: 10}}}}
target_steps: {target}
checkpoint_interval_seconds: 3600
limits: {{max_gpus: 1}}
{extra}""")


def run(session, exp, token):
    r = Runner(exp, RuntimeEnv(region="us-east-1", job_id="ur10", image_digest="sha256:" + "e" * 64,
                               fencing_token=token), session=session, install_signal_handlers=False)
    return r, r.run()


def test_training_iterations_checkpoint_and_artifacts(platform, fake_isaaclab):
    seed_external(platform, "ur10")
    r, outcome = run(platform, experiment(target=3), token=1)
    assert outcome == COMPLETED and r.steps == 3
    assert r.policy.iteration == 30
    assert fake_isaaclab[0].init_flags == [True, False, False]  # random episode lengths only at the start
    assert r.metrics["mean_reward"] == -0.75 and r.metrics["iteration"] == 30
    assert r.metrics["fps"] == round(24 * 64 / 0.3, 1)
    s3 = platform.client("s3")
    prefix = "results/ur10-unit/ur10/shard-00000/attempt-000001"
    spec = json.loads(s3.get_object(Bucket="research", Key=f"{prefix}/deploy/spec.json")["Body"].read())
    assert spec["joint_names"] == JOINTS and spec["default_joint_pos"] == DEFAULTS
    assert spec["actions"][0]["scale"] == 0.5 and spec["control_period_s"] == pytest.approx(1 / 30)
    assert [o["dim"] for o in spec["observations"]] == [6, 6, 7, 6]
    assert s3.get_object(Bucket="research", Key=f"{prefix}/policy/policy.onnx")["Body"].read() == b"onnx-bytes"
    assert FakeEnv.closed


def test_resume_restores_learner_and_continues_iterations(platform, fake_isaaclab):
    seed_external(platform, "ur10")
    run(platform, experiment(target=2), token=1)
    platform.resource("dynamodb").Table("control").update_item(
        Key={"PK": "JOB#ur10", "SK": "META"}, UpdateExpression="SET fencing_token = :t",
        ExpressionAttributeValues={":t": 2})
    r, outcome = run(platform, experiment(target=4, extra="resume: {allow_config_change: true}\n"), token=2)
    assert outcome == COMPLETED
    assert FakeRunner.loads == 1
    assert r.policy.iteration == 40 and r.steps == 4
    assert fake_isaaclab[-1].weights == 40.0  # 20 restored + 20 new, not restarted from zero
    assert fake_isaaclab[-1].init_flags == [False, False]


def test_resume_refuses_different_env_count(platform, fake_isaaclab):
    seed_external(platform, "ur10")
    run(platform, experiment(target=1), token=1)
    platform.resource("dynamodb").Table("control").update_item(
        Key={"PK": "JOB#ur10", "SK": "META"}, UpdateExpression="SET fencing_token = :t",
        ExpressionAttributeValues={":t": 2})
    _, outcome = run(platform, experiment(target=2, num_envs=128, extra="resume: {allow_config_change: true}\n"),
                     token=2)
    assert outcome == INCOMPATIBLE


@pytest.mark.parametrize("params, message", [
    ({}, "params.task"),
    ({"task": "Reach"}, "params.task"),
    ({"task": "Isaac-Reach-UR10-v0", "num_envs": 0}, "num_envs"),
    ({"task": "Isaac-Reach-UR10-v0", "device": "gpu"}, "device"),
    ({"task": "Isaac-Reach-UR10-v0", "typo": 1}, "unknown"),
])
def test_adapter_param_validation_needs_no_simulator(params, message):
    with pytest.raises(ValueError, match=message):
        ADAPTERS.create("isaaclab", params, seed=0)


def test_adapter_rejects_per_step_policies():
    adapter = ADAPTERS.create("isaaclab", {"task": "Isaac-Reach-UR10-v0"}, seed=0)
    with pytest.raises(NotImplementedError):
        adapter.step(0)


@pytest.mark.parametrize("params", [{"iterations_per_step": 0}, {"overrides": ["x"]}, {"lr": 1}])
def test_policy_param_validation(params):
    with pytest.raises(ValueError):
        POLICIES.create("rsl_rl_ppo", params, seed=0)


def test_dotted_overrides_reject_unknown_fields():
    cfg = type("Cfg", (), {})()
    cfg.algorithm = type("Alg", (), {"learning_rate": 1e-3})()
    apply_overrides(cfg, {"algorithm.learning_rate": 3e-4})
    assert cfg.algorithm.learning_rate == 3e-4
    with pytest.raises(ValueError, match="unknown config field"):
        apply_overrides(cfg, {"algorithm.lr": 1})
