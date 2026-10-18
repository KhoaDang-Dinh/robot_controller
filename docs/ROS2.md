# Running a trained policy on the UR10 (ROS 2 Jazzy)

```
cloud (EKS, GPU)                               lab PC (next to the robot)
Isaac Lab training  ── results/<exp>/<job>/  ──>  robot-ros2 image
                       policy/policy.onnx         ur_robot_driver  <-> UR10 / URSim / mock
                       deploy/spec.json           policy_node (ONNX, CPU, 30 Hz)
```

The control loop never goes through the cloud. The robot-side image
(`ghcr.io/khoadang-dinh/robot-ros2`, `containers/Dockerfile.ros2`) is ROS 2 **Jazzy**
(Ubuntu 24.04, recommended by Isaac Sim's ROS docs) with the Universal Robots ROS 2 driver
(`ros-jazzy-ur`) and the `robot_controller_ros` package in `ros2_ws/src/`.

## The policy node

`ros2_ws/src/robot_controller_ros`:

| File | Role |
|---|---|
| `policy_core.py` | Pure numpy: builds the exact Isaac Lab observation and maps actions to joint targets with safety layers (unit-tested) |
| `onnx_policy.py` | Runs `policy/policy.onnx` with onnxruntime on CPU |
| `policy_node.py` | rclpy node: `/joint_states` + `~/target_pose` in, `/forward_position_controller/commands` out |
| `launch/bringup.launch.py` | ur_robot_driver (`forward_position_controller`) + the node |
| `config/ur10_reach_spec.json` | UR10 reach conventions copied from Isaac Lab v2.3.2; a training run's `deploy/spec.json` replaces it |

Observation (25 values, in this order): `q - q_default`, `dq`, target pose
`[x, y, z, qw, qx, qy, qz]` in the robot base frame, then the previous raw action.
Action: `q_target = q_default + 0.5 * a`. ROS quaternions are `x, y, z, w`, so
the node reorders them.

**Safety layers.** The node starts **disarmed**: it publishes only `~/proposed_command`
until `~/arm` is called. While armed, every tick (each layer fails closed):

- holds if joint state is stale (>0.1 s), incomplete, or has non-finite values;
- holds if there is no target, or the policy output is NaN, the wrong shape or out of bounds;
- clamps targets to the UR10 joint limits;
- limits each command to `max_joint_step` (0.05 rad per tick) from the **measured** position.

"Holding" means publishing nothing, so the position controller keeps its last,
near-current command. The UR safety configuration and the e-stop remain the real
safety system.

## Run it

```bash
# 1. Fetch the artifacts of a finished training attempt
aws s3 cp s3://<research-bucket>/results/ur10-reach-ppo/<job>/shard-00000/attempt-000001/ ./policies/ --recursive

# 2. Mock hardware (no robot): checks wiring, frames and rates
docker run --rm -it --network host -v $PWD/policies:/policies:ro ghcr.io/khoadang-dinh/robot-ros2@sha256:<digest> \
  ros2 launch robot_controller_ros bringup.launch.py \
    policy_path:=/policies/policy/policy.onnx spec_path:=/policies/deploy/spec.json

# 3. Send a target in the base frame and watch the dry run
ros2 topic pub -r 5 /policy_controller/target_pose geometry_msgs/msg/PoseStamped \
  "{header: {frame_id: base_link}, pose: {position: {x: 0.5, y: 0.0, z: 0.3}, orientation: {x: 0.0, y: 0.7071068, z: 0.0, w: 0.7071068}}}"
ros2 topic echo /policy_controller/proposed_command
ros2 topic echo /policy_controller/status

# 4. URSim, then the real UR10: use_mock_hardware:=false robot_ip:=<ip>
#    (External Control URCap running on the robot), then arm:
ros2 service call /policy_controller/arm std_srvs/srv/SetBool "{data: true}"
```

## Check before arming on the real robot

1. **Frames.** Targets must be in the frame the policy was trained in (the Isaac Lab
   robot root). ROS `base_link` is the default, but UR descriptions have both `base` and
   `base_link`, rotated 180° about z. Compare forward kinematics for one joint
   configuration in sim and in ROS (`ros2 run tf2_ros tf2_echo base_link tool0`).
   The reach task tracks the `ee_link` body.
2. **Joint order and defaults.** Use the run's `deploy/spec.json`. The node maps
   joints by name in both directions.
3. **Rate.** The node ticks at the spec's control period (30 Hz for UR10 reach). Watch
   `~/status` for holds and clamping.
4. **Speed.** Start with a smaller `max_joint_step` and reduced speed on the teach
   pendant.
5. **Domain gap.** Simulated actuator gains, latency and joint-state noise differ from
   the real arm. Domain randomisation in the training task is the usual fix.

## CI

`ros2.yml` builds the image and runs the package tests (`colcon test`). It then runs
`bringup.launch.py` on ur_robot_driver's **mock hardware** with a generated test policy.
It asserts that nothing reaches the controller while disarmed and that commands flow
after arming. Then it scans the image with Trivy, and pushes it from `dev`.

The Isaac Sim ROS 2 bridge (sim-in-the-loop against the same node) is a later step.
Isaac Lab's own ROS 2 container is noted as broken after its Python 3.11 move, which
is one reason training and robot images are separate.
