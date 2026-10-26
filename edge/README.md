# ROS/Jetson edge transport v1 (opt-in)

This **adds a transport implementation**, not a ROS driver or an automatically installed
Greengrass component. A ROS 1, ROS 2 or Isaac Sim adapter calls `build_event` and
`EventOutbox.enqueue` off the control loop. Only important small operational events
go to AWS IoT Core Basic Ingest -> SQS. Full logs, closed MCAP/rosbags, LiDAR files,
camera clips and simulation records go directly to S3 as immutable payload + manifest.
The manifest object is written **last** and is the indexing trigger.

Requirements for the separate edge process: Python 3.10+, `boto3` and
`paho-mqtt>=1.6,<3`. This is **not** a promise of native compatibility with the
legacy JetPack 4 system Python on Nano/TX2; use a compatible container/runtime
and benchmark CPU/RAM. Avoid changing existing ROS 1 installations.

Example (from repo root; set IDs, endpoints and certificates for your lab):

```bash
python3 -m edge.cli --device-id jetson-tx2-001 --robot-id ur10-lab-01 event \
  --type model.inference_failed --severity ERROR --message "CUDA OOM" \
  --data '{"error_code":"MODEL_MEMORY_EXHAUSTED"}'
python3 -m edge.cli --device-id jetson-tx2-001 --robot-id ur10-lab-01 flush \
  --rule robot_sim_dev_robot_events --mqtt-endpoint YOUR_DATA_ENDPOINT \
  --ca AmazonRootCA1.pem --cert thing.pem.crt --key thing.pem.key
python3 -m edge.cli --device-id jetson-tx2-001 --robot-id ur10-lab-01 upload \
  --bucket YOUR_RESEARCH_BUCKET --region ap-southeast-1 \
  --file closed-recording.mcap --kind rosbag --run-id run001 \
  --credentials-endpoint YOUR_CREDENTIALS_ENDPOINT --role-alias YOUR_ROLE_ALIAS \
  --ca AmazonRootCA1.pem --cert thing.pem.crt --key thing.pem.key
```

* The `device-id` must equal the AWS IoT Thing name and certificate attachment
  when using the IAM per-device S3 prefix. Provision certificates outside Git/Terraform.
* `event` fsyncs a bounded offline queue; `flush` waits for MQTT QoS1 PUBACK
  before removing a file. SQS and consumers still need dedupe by `event_id`.
  The CLI flush operation can be periodically launched by systemd or Greengrass.
* Do not put raw imagery, ROS messages at servo frequency, large traces, or
  personal data in MQTT operational events. Do not transport robot emergency
  stops via AWS; safety must stay on local hardware.
* `upload` requires a closed, nonempty file. It hashes locally, uses an
  idempotent content-addressed key, checks object size/metadata, and writes
  `*.manifest.json` last. A failed upload leaves the local input file intact.
  Consumer verification should check the actual S3 payload SHA-256.
* MQTT metrics/state, Device Shadow capability registration, active command
  handling, local log rotation, automatic S3 retry scheduling and Greengrass
  deployment are **follow-up integrations**, not claimed live in this patch.
* Credentials: prefer AWS IoT Credentials Provider + Role Alias. On AWS EC2,
  omit IoT credentials flags to use normal instance role credentials. Never
  commit thing private keys or put them in CLI output.
* See `infra_robot_sim/infra/environments/dev/base/edge_iot.tf` for the optional
  IoT policy, Basic Ingest -> SQS route and per-device S3 role.
