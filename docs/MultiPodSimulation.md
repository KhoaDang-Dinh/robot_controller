# Multi-pod robot simulation capability (Phase 1+)

## Scope

Run **N independent Isaac Sim simulation shards** as an EKS Indexed Job,
with up to **P concurrently scheduled GPU Pods** (P <= N).
The first implementation includes a real checkpoint/recovery smoke container,
not a trained Isaac policy or synchronous DDP across pods.

* Replica ownership: JOB_COMPLETION_INDEX (0 ... N-1).
* Simulation progress: S3 under jobs/<job-id>-shard-<index>/attempts/<fence>/.
* Durable pointer/status: DynamoDB JOB#<job-id>-shard-<index>, SK=META.
* Split workloads by fixed seeds/episode IDs based on shard index.
* A new attempt increments fencing_token; outdated workers cannot advance
  the DynamoDB pointer. Distinct fence-scoped S3 keys cannot overwrite a
  newer attempt's checkpoint payload.
* GPU: one NVIDIA resource per pod by default, no GPU sharing assumed.
* Job progress and infrastructure state remain separate from etcd.
* Container image is pinned by SHA256; a GHCR tag update will NOT restart
  or mutate existing running Pods.

## Architecture

GitHub Actions/self-hosted runner
  -> Terraform (base persistent, EKS disposable)
  -> Cilium networking + Argo CD for platform components
  -> Helm per-experiment Indexed Job
  -> GPU Spot or On-Demand node group
  -> per-Pod S3 checkpoint + per-shard DynamoDB fencing
  -> S3 telemetry/artifacts / AWS IoT (app integration later)

## Modes

1. shards=1 / parallelism=1: existing resource-light development path.
2. shards=8 / parallelism=2: 8 independent experiments, 2 at once.
3. shards=32 / parallelism=4: 32 experiments, up to 4 GPU Pods (requires
   appropriate node group max/desired capacity and GPU quotas).
4. Optional strict synchronized simulation: set synchronization.enabled=true,
   shards=parallelism, with enough GPUs for every shard. Each round uses a
   fenced S3 checkpoint and DynamoDB barrier. A Spot node loss pauses peers
   while a replacement shard restores and rejoins, subject to timeout.
5. Optional future: torchrun/DDP synchronized learning from experience
   collected by shard Pods. DDP is NOT implemented in this chart.

## Cluster lifecycle

The reusable cluster controllers may be GitOps managed. Do **not** have
Argo CD continuously re-apply an experiment Job when it reaches Completed.
Job runs are versioned/immutable, and a new Kubernetes cluster can recreate
the Job after restoring from S3/DynamoDB.

On Spot eviction, SIGTERM allows an orderly checkpoint; the worker also
checkpoints periodically. After an unclean termination, the Job controller
may restart a failed shard. The new pod gets a new fencing token and loads
the last committed manifest. A new cluster uses the same ID and shard
assignment to resume from the last checkpoint.

For Isaac Sim, the worker application must serialize environment state,
assets/references, RNG, policies, optimizers and episode cursors, and must
validate its image+USD asset revisions during recovery. The included smoke
worker only stores a progress counter and is not a substitute for that.

## Explicit cost controls

The existing GPU node groups default to min=0 and desired=0.
A Job alone cannot create GPU capacity without an autoscaler. Increase
Terraform gpu_max_size when necessary and explicitly set the node group
desired size. Keep parallelism, per-pod GPU requests, node group desired/max,
AWS GPU quota and experiment cost cap consistent. On-Demand scheduling must
be an explicit choice, not silent Spot replacement.

A live EKS control plane, CPU workers and NAT Gateway have hourly costs;
only the stop/destroy lifecycle returns the design to its lower idle-cost
persistent base.

## Non-goals

No exactly-once execution guarantee for simulation side effects; avoid
unsafe external side effects during worker retries. No dynamic job scaling
of already-created Indexed Jobs; do not change shards mid-experiment.
No Cilium ClusterMesh, multi-region failover, DDP gradient sync or
synchronized optimizer checkpoint coordinator yet. The optional barrier
only synchronizes simulator logical steps; it does not synchronize policy
weights, gradients or replay buffers.
