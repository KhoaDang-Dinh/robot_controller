import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("hello", Path(__file__).resolve().parents[2]/"scripts/hello_k8s.py")
hello = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hello)


class LifecycleTest(unittest.TestCase):
    def execute(self, mode):
        commands = []
        def cli(command, **kwargs):
            commands.append(command)
            if "create" in command:
                manifest = json.loads(kwargs["input"])
                self.assertEqual(manifest["spec"]["backoffLimit"], 0)
                return '{"metadata":{"name":"robot-hello-test"}}'
            if "wait" in command and mode == "timeout":
                raise subprocess.CalledProcessError(1, command)
            if "logs" in command:
                return "WRONG\n" if mode == "bad-marker" else "HELLO_ROBOT_K8S\n"
            return ""
        with patch("sys.argv", ["hello", "--context", "test-cluster"]), patch.object(hello.subprocess, "check_output", side_effect=cli):
            if mode == "success":
                hello.main()
            else:
                with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                    hello.main()
        self.assertIn("delete", commands[-1])
        self.assertIn("robot-hello-test", commands[-1])
        for command in commands:
            self.assertEqual(command[command.index("--context")+1], "test-cluster")
    def test_success(self):
        self.execute("success")
    def test_timeout_cleans_up(self):
        self.execute("timeout")
    def test_wrong_marker_fails(self):
        self.execute("bad-marker")
