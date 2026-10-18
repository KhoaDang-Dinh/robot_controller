"""ROS 2 node: run a trained Isaac Lab reach policy against ur_robot_driver.

Inputs
  /joint_states                 sensor_msgs/JointState   (from ur_robot_driver)
  ~/target_pose                 geometry_msgs/PoseStamped (frame_id must be ``base_frame``)
Outputs
  ``command_topic``             std_msgs/Float64MultiArray, ONLY while armed
                                (default /forward_position_controller/commands)
  ~/proposed_command            std_msgs/Float64MultiArray, always (dry-run view)
  ~/status                      std_msgs/String, JSON once per second
Services
  ~/arm                         std_srvs/SetBool   arm / disarm
  ~/clear_target                std_srvs/Trigger   stop tracking (hold position)

The node starts DISARMED. It never commands a joint beyond its limits, and never
more than ``max_joint_step`` from the measured position in one tick. When an input
is stale or missing it publishes nothing, so the position controller holds its last
(near-current) command. This is not a safety system: keep the UR safety
configuration and the e-stop in place.
"""
from __future__ import annotations

import json
import os

from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import PoseStamped
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray, String
from std_srvs.srv import SetBool, Trigger

from robot_controller_ros.policy_core import (
    UR10_LOWER, UR10_UPPER, Command, DeploySpec, PolicyController, SafetyLimits, reorder, ros_pose_to_command,
)

UR_JOINTS = ["shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint",
             "wrist_1_joint", "wrist_2_joint", "wrist_3_joint"]


class PolicyNode(Node):
    def __init__(self):
        super().__init__("policy_controller")
        default_spec = os.path.join(get_package_share_directory("robot_controller_ros"),
                                    "config", "ur10_reach_spec.json")
        p = self.declare_parameter
        spec_path = p("spec_path", default_spec).value
        policy_path = p("policy_path", "").value
        self.armed = bool(p("armed", False).value)
        self.base_frame = p("base_frame", "base_link").value
        command_topic = p("command_topic", "/forward_position_controller/commands").value
        self.controller_joints = list(p("controller_joints", UR_JOINTS).value)
        limits = SafetyLimits(
            lower=np.asarray(p("joint_lower", list(UR10_LOWER)).value, dtype=float),
            upper=np.asarray(p("joint_upper", list(UR10_UPPER)).value, dtype=float),
            max_step_rad=float(p("max_joint_step", 0.05).value),
            joint_state_timeout_s=float(p("joint_state_timeout", 0.1).value),
            target_timeout_s=float(p("target_timeout", 0.0).value),
            zero_nonfinite_velocity=bool(p("mock_hardware", False).value),
        )
        if not policy_path:
            raise RuntimeError("parameter policy_path is required (policy/policy.onnx from a training run)")

        from robot_controller_ros.onnx_policy import OnnxPolicy

        self.spec = DeploySpec.load(spec_path)
        policy = OnnxPolicy(policy_path, self.spec.observation_dim)
        self.core = PolicyController(self.spec, policy, limits)
        reorder(np.zeros(len(self.spec.joint_names)), self.spec.joint_names, self.controller_joints)  # validate

        self.command_pub = self.create_publisher(Float64MultiArray, command_topic, 10)
        self.proposed_pub = self.create_publisher(Float64MultiArray, "~/proposed_command", 10)
        self.status_pub = self.create_publisher(String, "~/status", 10)
        self.create_subscription(JointState, "/joint_states", self.on_joint_state, qos_profile_sensor_data)
        self.create_subscription(PoseStamped, "~/target_pose", self.on_target, 10)
        self.create_service(SetBool, "~/arm", self.on_arm)
        self.create_service(Trigger, "~/clear_target", self.on_clear)
        self.create_timer(self.spec.control_period_s, self.on_tick)
        self.create_timer(1.0, self.publish_status)
        self.last = {"result": "starting"}
        self.ticks = {"commands": 0, "holds": 0, "clamped": 0, "rate_limited": 0}
        self.get_logger().info(
            f"task={self.spec.task} period={self.spec.control_period_s:.4f}s armed={self.armed} "
            f"publishing to {command_topic} only when armed")

    def now(self) -> float:
        return self.get_clock().now().nanoseconds * 1e-9

    def on_joint_state(self, msg: JointState) -> None:
        self.core.update_joint_state(msg.name, msg.position, msg.velocity, self.now())

    def on_target(self, msg: PoseStamped) -> None:
        if msg.header.frame_id != self.base_frame:
            self.get_logger().warn(
                f"ignoring target in frame {msg.header.frame_id!r}; expected {self.base_frame!r}",
                throttle_duration_sec=5.0)
            return
        o, q = msg.pose.position, msg.pose.orientation
        try:
            self.core.update_target(ros_pose_to_command((o.x, o.y, o.z), (q.x, q.y, q.z, q.w)), self.now())
        except ValueError as error:
            self.get_logger().warn(f"ignoring target: {error}")

    def on_arm(self, request: SetBool.Request, response: SetBool.Response) -> SetBool.Response:
        self.armed = bool(request.data)
        response.success, response.message = True, f"armed={self.armed}"
        self.get_logger().warn(response.message)
        return response

    def on_clear(self, request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        self.core.clear_target()
        response.success, response.message = True, "target cleared; holding"
        return response

    def on_tick(self) -> None:
        result = self.core.tick(self.now())
        if not isinstance(result, Command):
            self.ticks["holds"] += 1
            self.last = {"result": "hold", "reason": result.reason}
            return
        ordered = reorder(result.positions, self.spec.joint_names, self.controller_joints)
        msg = Float64MultiArray(data=ordered)
        self.proposed_pub.publish(msg)
        if self.armed:
            self.command_pub.publish(msg)
        self.ticks["commands"] += 1
        self.ticks["clamped"] += int(result.clamped)
        self.ticks["rate_limited"] += int(result.rate_limited)
        self.last = {"result": "command", "armed": self.armed, "clamped": result.clamped,
                     "rate_limited": result.rate_limited}

    def publish_status(self) -> None:
        self.status_pub.publish(String(data=json.dumps({"armed": self.armed, **self.last, **self.ticks})))


def main(args=None) -> None:
    rclpy.init(args=args)
    node = PolicyNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
