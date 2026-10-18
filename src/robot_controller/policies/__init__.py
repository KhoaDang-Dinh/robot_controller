"""Policies. Importing this package registers every built-in policy."""
from robot_controller.plugins import Registry

POLICIES = Registry("policy")

from robot_controller.policies import epsilon_greedy, noop  # noqa: E402,F401
