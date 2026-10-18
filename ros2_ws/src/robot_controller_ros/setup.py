from glob import glob

from setuptools import find_packages, setup

package_name = "robot_controller_ros"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/launch", glob("launch/*.launch.py")),
        (f"share/{package_name}/config", glob("config/*")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="KhoaDang-Dinh",
    description="Run Isaac Lab policies trained by robot_controller on a UR10 through ur_robot_driver.",
    license="NOASSERTION",
    tests_require=["pytest"],
    entry_points={"console_scripts": ["policy_node = robot_controller_ros.policy_node:main"]},
)
