"""Write a tiny ONNX policy (linear, small gains) with the Isaac Lab exporter's I/O names.

Used by the mock-hardware integration test; needs the ``onnx`` package.
    python3 test/make_test_policy.py /tmp/policy.onnx
"""
import sys

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper


def main(path: str, obs_dim: int = 25, act_dim: int = 6) -> None:
    rng = np.random.default_rng(0)
    weights = (rng.standard_normal((obs_dim, act_dim)) * 0.05).astype(np.float32)
    graph = helper.make_graph(
        [helper.make_node("MatMul", ["obs", "W"], ["actions"])],
        "test_policy",
        [helper.make_tensor_value_info("obs", TensorProto.FLOAT, [1, obs_dim])],
        [helper.make_tensor_value_info("actions", TensorProto.FLOAT, [1, act_dim])],
        initializer=[numpy_helper.from_array(weights, "W")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    onnx.save(model, path)


if __name__ == "__main__":
    main(sys.argv[1])
