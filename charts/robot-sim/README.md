# Robot simulation multi-pod Helm chart

This is an **opt-in**, per-experiment Indexed Kubernetes Job. It is NOT an
Argo CD continuously-reconciled Job. Argo CD manages the reusable platform;
the experiment controller/operator installs one immutable Job per run.

The default image is a **checkpointed smoke worker**, not NVIDIA Isaac Sim.
Use it to prove N independent shards can resume after the EKS cluster is
destroyed. A production Isaac Sim container must implement the same per-shard
JOB_COMPLETION_INDEX, fenced DynamoDB and S3 checkpoint contract.

One shard == one independently restorable simulation stream. A shard does not
need to be on the same node, GPU, or AWS instance on its next attempt.

Prerequisites:
1. Disposable dev EKS runtime started; Pod Identity agent and Cilium healthy.
2. Terraform applied with GPU node-group max size adequate for desired pods.
3. GPU node group explicitly scaled above zero; no cluster autoscaler is
   assumed. Each default g5.xlarge gives one full nvidia.com/gpu resource.
4. Image built and pushed to GHCR; immutable digest available. Configure
   the ghcr-pull Secret if package is private.
5. /robot-sim/dev/runtime/desired-state in SSM equals running.
6. IAM Pod Identity association for robot-sim-worker applied.

Shards, parallelism, GPUs per shard, deadline and barrier settings come from the
experiment file (see docs/EXPERIMENTS.md); the chart decides where Pods run.

Example (after scaling the gpu-spot node group to 2 nodes):

    robot-run validate experiments/smoke-multipod.yaml
    helm install sim-trial-001 charts/robot-sim \
      --namespace robot-platform --create-namespace \
      --set enabled=true --set job.id=trial-001 \
      --set-file experiment=experiments/smoke-multipod.yaml \
      --set image.digest=sha256:REPLACE_WITH_64_HEX_DIGITS \
      --set image.pullSecrets[0].name=ghcr-pull

The experiment file is stored verbatim in a ConfigMap and its SHA-256 is put on
the Job and Pods (robot-sim/config-sha, robot-sim/config-sha256). Resuming the
same job.id with a different file is refused unless the file sets
resume.allow_config_change: true.

Monitor:

    kubectl -n robot-platform get job robot-sim-trial-001
    kubectl -n robot-platform get pods -l robot-sim/job-id=trial-001
    kubectl -n robot-platform logs -l robot-sim/job-id=trial-001 --all-containers --prefix --max-log-requests=10

For Spot to On-Demand fallback, reschedule a NEW Job release using
--set scheduling.purchase=on-demand, after stopping and checkpointing the
old workload. This is an explicit cost decision, not automatic failover.

Before destroying the runtime, set desired-state=stopping and wait for all
shard records to be SUSPENDED or COMPLETED. The existing shutdown workflow
checks these records (including paginated DynamoDB reads). Force bypasses
graceful wait and recovers only the most recently committed checkpoint.

For resume after destroying/recreating EKS, use the same job.id, shards,
and immutable image/scene configuration, with a new Helm release in the new
cluster. Already completed shards will restore their last step and exit.
Do not change shard count while reusing the job ID.

If a pod is hard-killed, progress since its last checkpoint is lost.
S3 state is not equivalent to an in-memory live migration. Never write
mutable global checkpoints from multiple shards into the same S3 path.


## Synchronized simulation with EC2 Spot loss recovery

The default mode has independent Pods and supports parallelism < shards.
Optional **strict step synchronization** requires one runnable GPU Pod per
shard (all shards have to reach each round). In the experiment file:

    shards: 2
    parallelism: 2
    sync: {every_steps: 10}

**Important:** for shards=2 you need at least 2 *available* GPUs. A Job with
only 1 GPU will deadlock waiting for a Pod that cannot start; Helm detects
parallelism mismatch but cannot detect actual AWS free GPU capacity.
Use available GPU capacity and test before increasing N.

Every Pod commits to its own S3 checkpoint, then publishes a fenced
barrier_round to its own DynamoDB shard item. It can advance past that round
only after all peers reach it. A terminated Spot VM causes its shard to
restart on another GPU; that Pod restores its checkpoint, takes a new fence,
and rejoins the round. Existing Pods wait instead of restarting.

The barrier has a timeout (default 1800s). AWS capacity is not guaranteed:
if a replacement VM does not arrive before timeout, the Job retry budget
can be exhausted and the run fails safely rather than making progress with
unverified peers. Operator stop breaks the wait, commits SUSPENDED and then
allows the daily runtime teardown. Force destroy recovers the last committed
checkpoint and may lose work after that checkpoint.

This is a **simulation-step barrier**, not PyTorch DDP or synchronized
optimizer/gradient updates. Real Isaac Sim integration must serialize and
restore sim state. For high-throughput training prefer loosely coupled
simulator actors plus a durable replay buffer and a dedicated learner;
strict barriers reduce throughput if a Spot node is interrupted.
