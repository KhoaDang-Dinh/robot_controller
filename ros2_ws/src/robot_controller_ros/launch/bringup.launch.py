"""UR10 + trained policy.

Mock hardware (no robot, CI and desk testing):
    ros2 launch robot_controller_ros bringup.launch.py policy_path:=/policies/policy.onnx
URSim or the real UR10 (robot must run the External Control URCap):
    ros2 launch robot_controller_ros bringup.launch.py use_mock_hardware:=false \
        robot_ip:=192.168.56.101 policy_path:=/policies/policy.onnx spec_path:=/policies/spec.json

The policy node starts disarmed: watch ~/proposed_command, then
    ros2 service call /policy_controller/arm std_srvs/srv/SetBool "{data: true}"
"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    args = [
        DeclareLaunchArgument("ur_type", default_value="ur10"),
        DeclareLaunchArgument("robot_ip", default_value="192.168.56.101"),
        DeclareLaunchArgument("use_mock_hardware", default_value="true"),
        DeclareLaunchArgument("launch_rviz", default_value="false"),
        DeclareLaunchArgument("policy_path", description="policy/policy.onnx from a training run"),
        DeclareLaunchArgument(
            "spec_path",
            default_value=PathJoinSubstitution([FindPackageShare("robot_controller_ros"), "config",
                                                "ur10_reach_spec.json"]),
            description="deploy/spec.json from the same training run (authoritative)"),
        DeclareLaunchArgument("armed", default_value="false"),
    ]
    driver = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution([FindPackageShare("ur_robot_driver"), "launch", "ur_control.launch.py"])),
        launch_arguments={
            "ur_type": LaunchConfiguration("ur_type"),
            "robot_ip": LaunchConfiguration("robot_ip"),
            "use_mock_hardware": LaunchConfiguration("use_mock_hardware"),
            "launch_rviz": LaunchConfiguration("launch_rviz"),
            # The policy outputs joint position targets every control period.
            "initial_joint_controller": "forward_position_controller",
        }.items(),
    )
    policy = Node(
        package="robot_controller_ros",
        executable="policy_node",
        name="policy_controller",
        output="screen",
        parameters=[
            PathJoinSubstitution([FindPackageShare("robot_controller_ros"), "config", "policy_node.yaml"]),
            {
                "policy_path": LaunchConfiguration("policy_path"),
                "spec_path": LaunchConfiguration("spec_path"),
                "armed": LaunchConfiguration("armed"),
                "mock_hardware": LaunchConfiguration("use_mock_hardware"),
            },
        ],
    )
    return LaunchDescription([*args, driver, policy])
