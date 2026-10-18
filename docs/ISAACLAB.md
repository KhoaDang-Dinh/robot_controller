# Training on Isaac Lab

| Piece | Pinned | Where |
|---|---|---|
| Isaac Sim | 5.1.0 (`nvcr.io/nvidia/isaac-sim:5.1.0`, Python 3.11) | `containers/Dockerfile.isaaclab` |
| Isaac Lab | v2.3.2, commit `37ddf62…` (the build fails if the tag moves) | same |
| RL library | rsl_rl (Isaac Lab's `--install rsl_rl` extra) | same |
| Image | `ghcr.io/khoadang-dinh/robot-sim-isaaclab` | `build-isaaclab.yml` |

Isaac Lab v2.3.2 is the latest stable release (the 3.0 line is still beta). Isaac Sim
and Isaac Lab versions only work in matching pairs, so change both together and check
NVIDIA's compatibility notes and the GPU driver requirements first.

## How it plugs into the runner

```
experiments/ur10-reach-ppo.yaml
  adapter: isaaclab      -> starts headless Isaac Sim, gym.make("Isaac-Reach-UR10-v0", 4096 envs)
  policy:  rsl_rl_ppo    -> rsl_rl OnPolicyRunner with the task's registered PPO config
core/runner.py           -> fencing, checkpoints, SIGTERM/Spot, results, telemetry (unchanged)
```

* One runner **step** is `iterations_per_step` PPO iterations (default 10), so
  `target_steps: 100` gives Isaac Lab's default 1000 iterations for UR10 reach.
* **Checkpoints** hold the learner (weights including observation normalisers,
  optimizer state, iteration count, torch RNG), not the simulator. On resume the 4096 envs
  are reset, so at most one partial rollout is lost. Resume is not bit-identical,
  because GPU physics is not deterministic, so compare recovered and uninterrupted runs
  statistically.
* **Resume guard**: a different task, a different `num_envs`, or a changed experiment
  file is refused. A Spot replacement with the same file continues exactly where it stopped.
* **Artifacts** written next to the results at the end of each attempt:
  `policy/policy.onnx`, `policy/policy.pt` (TorchScript), and `deploy/spec.json`.
  The spec records joint names, default joint positions, action scale, control period
  and observation layout from the live env. The ROS 2 node runs from these files.
* **Metrics**: `mean_reward`, `mean_episode_length`, losses, learning rate and fps.
  They go into telemetry, checkpoint metadata and results. They are per-chunk
  approximations; rsl_rl's TensorBoard files under `/tmp/rsl_rl` in the Pod are the
  exact curves.

Tune it from the experiment file, not code:

```yaml
policy:
  name: rsl_rl_ppo
  params:
    iterations_per_step: 10
    overrides: {algorithm.learning_rate: 3.0e-4, policy.actor_hidden_dims: [128, 128]}
adapter:
  name: isaaclab
  params: {task: Isaac-Reach-UR10-v0, num_envs: 4096, env_overrides: {episode_length_s: 8.0}}
```

## Running on the dev EKS runtime (cloud first)

Infra side (infra_robot_sim, see its `docs/GPU_TRAINING.md`): g5.2xlarge GPU pools
(Isaac Sim 5.1 needs at least 32 GB host RAM), the NVIDIA device plugin, and a
**Scale dev GPU pool** workflow.

1. **Image**: once this lands on `dev`, **Build Isaac Lab training image** runs on its
   own. It can also be started by hand from `dev`. Copy the digest from the job summary.
   If the hosted runner runs out of disk, set `ISAACLAB_BUILD_RUNNER` (see the workflow header).
2. **Runtime**: in infra_robot_sim run **Start dev EKS runtime**, then **Scale dev GPU pool**
   (`gpu-spot`, desired = the experiment's `parallelism`, 2 for `ur10-reach-ppo`).
3. **Train**: run **Train on dev EKS** here, from `dev`:
   `action=install`, `experiment=experiments/ur10-reach-ppo.yaml`, a new `job_id`, the
   image digest, and the NVIDIA EULA checkbox. The workflow uses the scoped deploy role
   and waits until Pods are Running. It warns if no GPU is schedulable (pool not scaled,
   or the device plugin is not running) and writes status and telemetry to the summary.
4. **Follow**: `action=status` with the same `job_id`. Results and artifacts land in
   `s3://<research bucket>/results/ur10-reach-ppo/<job_id>/`.
5. **Resume**: after an interruption that exhausted the Job's retries, use
   `action=reinstall` with the same `job_id` and digest. Shards continue from their
   last committed checkpoints.
6. **Stop paying**: **Scale dev GPU pool** to 0, then **Stop dev EKS runtime**.

The equivalent manual command:

```bash
helm install train-ur10-001 charts/robot-sim -n robot-platform \
  --set enabled=true --set job.id=ur10-001 \
  --set-file experiment=experiments/ur10-reach-ppo.yaml \
  --set image.repository=ghcr.io/khoadang-dinh/robot-sim-isaaclab \
  --set image.digest=sha256:<digest> --set isaacsim.acceptEula=true
```

The chart's Isaac Lab profile runs as Isaac Sim's rootless user (1234). It adds a
memory-backed `/dev/shm` and cache volumes under `/isaac-sim`, and keeps a writable root
filesystem (Isaac Sim writes into its install tree). It sets `ACCEPT_EULA=Y` only with
`isaacsim.acceptEula=true`, and never sets privacy/data-collection consent. Shader caches
are rebuilt on each Pod start. Set `isaacsim.cachePvc` to a PersistentVolumeClaim to keep
them between starts.

## What CI can and cannot check

CI has no GPU. `tests/unit/test_isaaclab_ppo.py` runs the real runner against fakes
that copy the Isaac Lab v2.3.2 / rsl_rl 3.1 surfaces the plug-ins use. That covers
iterations, checkpoints, resume, artifacts and the deployment spec.
`build-isaaclab.yml` checks that the image installs and validates the experiment.
The first real training run on a GPU node is the acceptance test for the
simulator path: record its digest, logs and results.
