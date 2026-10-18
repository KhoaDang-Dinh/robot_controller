#!/bin/bash
set -e
# shellcheck disable=SC1091
source /opt/ros/jazzy/setup.bash
# shellcheck disable=SC1091
source /ws/install/setup.bash
exec "$@"
