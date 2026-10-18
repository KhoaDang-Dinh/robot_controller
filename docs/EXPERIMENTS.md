# Experiments: running, adding and changing them

The repository has three layers, and each one changes at a different speed:

| Layer | Where | Changes | Gate |
|---|---|---|---|
| Core | `src/robot_controller/core/` | rarely | full suite, golden-checkpoint compat, kind e2e |
| Plug-ins | `src/robot_controller/adapters/`, `policies/` | often | that plug-in's unit tests + the suite |
| Experiments | `experiments/*.yaml` | constantly | `robot-run validate` (seconds) |

Trying something new should only touch the bottom two layers. The core
(`runner.py`) handles fencing, checkpoint cadence, SIGTERM/Spot shutdown, the
shard barrier, results and telemetry for every experiment. That way a new
idea cannot break recovery or cost tracking.

## What you change for each kind of work

| You want to… | You touch | Image rebuild? |
|---|---|---|
| Change hyperparameters, seeds, shards, budget | a copy of an `experiments/*.yaml` | no; the image reads the file at run time |
| Try a new learning or control approach | `policies/<idea>.py`, a test, an experiment file | yes (thin layer only) |
| Add a simulator (Isaac Sim + UR10) | `adapters/<sim>/`, and `BASE_IMAGE` in `containers/Dockerfile.app` | base image too |
| Change checkpointing or recovery | `core/` | yes; also add a golden checkpoint |

## An experiment file

```yaml
schema_version: 1
name: reach-epsilon-greedy        # = file name; becomes the robot-sim/experiment label
description: optional free text
adapter: {name: smoke, params: {target_position: 10, step_seconds: 0.1}}
policy:  {name: epsilon_greedy, params: {epsilon: 0.5, decay: 0.95}}
seeds: [1, 2, 3, 4]               # one per shard, or one value: shard i uses seed + i
shards: 4                         # independent streams (Indexed Job completions)
parallelism: 4                    # Pods at once; parallelism x gpus_per_shard <= max_gpus
target_steps: 2000                # 0 = run until stopped
checkpoint_interval_seconds: 60
sync: null                        # or {every_steps, poll_seconds, timeout_seconds}; needs parallelism == shards
limits: {max_gpus: 4, gpus_per_shard: 1, active_deadline_seconds: 3600}
resume: {allow_config_change: false}
```

Unknown keys are errors, so a typo fails validation instead of silently
falling back to a default on a GPU run. Check a file with
`uv run robot-run validate experiments/<file>.yaml`. That also builds the
adapter and policy with your params, so a bad parameter is caught here.

**Identity.** `config_sha256` is the SHA-256 of the file's exact bytes. The chart
puts it on the Job and Pods, and the runner writes it into every checkpoint and
result. Resuming the same `job.id` with a changed file is refused, unless the
file sets `resume.allow_config_change: true`. To change an experiment, run it
under a new `job.id`, or copy it to a new file. Leave results already recorded
under the old hash alone.

**Runtime overrides** that do not change identity: `CHECKPOINT_INTERVAL_SECONDS`
and `TARGET_STEPS` environment variables. `MAX_STEPS` is accepted as the old name
for `TARGET_STEPS`. Tests and operations use these.

## Running

Locally, against the AWS emulator (no account needed):

```bash
uv sync
uv run pytest tests/dynamic -q                 # real runner process vs moto server
```

On EKS (dev runtime started, image digest from the **Build and publish workload images** workflow):

```bash
uv run robot-run validate experiments/reach-epsilon-greedy.yaml
helm install reach-001 charts/robot-sim -n robot-platform \
  --set enabled=true --set job.id=reach-001 \
  --set-file experiment=experiments/reach-epsilon-greedy.yaml \
  --set image.digest=sha256:<digest> --set image.pullSecrets[0].name=ghcr-pull
```

Results land in `s3://<research bucket>/results/<experiment>/<job id>/shard-<i>/attempt-<token>.json`.
Telemetry goes to Pod logs and feeds `scripts/usage_report.py`.

## Adding a policy

1. Copy `policies/epsilon_greedy.py` to `policies/<idea>.py` and register it:
   `@POLICIES.register("<idea>")`.
2. Implement `act`, `learn`, `save_state` and `load_state`. `save_state()` must hold
   everything needed to continue identically: weights, optimizer state,
   RNG state and replay-buffer cursor.
3. Import it in `policies/__init__.py`.
4. Add `tests/unit/test_<idea>.py`. At minimum, test the save → load round trip
   and that it continues deterministically from the same seed.
5. Add `experiments/<name>.yaml` that uses it.

Bump `state_version` whenever the saved format changes. `load_state(files,
state_version)` receives the stored version. Convert old versions there, or raise
`CheckpointIncompatible`. The runner already refuses checkpoints from a *newer*
version, so a rollback never misreads data written by newer code.

## Adding a simulator adapter

The steps are the same, under `adapters/` with `@ADAPTERS.register(...)`:
`reset`, `observation`, `step`, `save_state`, `load_state`, `close`. For Isaac Sim:

* Put the USD scene and asset revision in `save_state()` and check it in `load_state()`.
* Change `BASE_IMAGE` in `containers/Dockerfile.app` to an Isaac Sim / CUDA image
  pinned by digest. Code changes then rebuild only the thin layers on top.
* Use `limits.gpus_per_shard >= 1`. The chart then schedules onto GPU nodes and
  requests `nvidia.com/gpu`.

## Compatibility rules

* `tests/compat/golden/` holds real checkpoints written by earlier releases.
  `tests/compat/` proves the current code resumes them, or refuses them on purpose.
* When you change the checkpoint layout or a `state_version`, record a new
  golden directory from the **previous** release. Do not edit existing ones.
* Exit codes: `0` completed (or a clean stop or fence for a single external-token Job),
  `1` stopped or fenced shard of an Indexed Job (so the Job is not marked done),
  `3` incompatible checkpoint, `4` runtime not in `running` state.

## Branches

Work on short-lived `exp/<idea>` or `feature/<thing>` branches off `dev`, and merge
what you want to keep. CI on every push runs the experiment validation (seconds),
then unit, compat and dynamic tests (minutes). The image build and Trivy gate
run on `dev` when code, dependencies or the baked-in default experiment
(`experiments/smoke-resume.yaml`) change. Other experiment files don't trigger a
rebuild, because the chart passes them in at install time.
