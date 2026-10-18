"""Simulator adapters. Importing this package registers every built-in adapter."""
from robot_controller.plugins import Registry

ADAPTERS = Registry("adapter")

from robot_controller.adapters import isaaclab, smoke  # noqa: E402,F401  (register adapters)
