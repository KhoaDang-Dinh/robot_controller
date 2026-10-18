"""Run an exported Isaac Lab / rsl_rl policy (policy/policy.onnx) with onnxruntime on CPU."""
from __future__ import annotations

import numpy as np


class OnnxPolicy:
    def __init__(self, path: str, observation_dim: int):
        import onnxruntime as ort  # imported lazily so the core stays testable without it

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1  # small MLP: one thread gives the lowest, steadiest latency
        self.session = ort.InferenceSession(path, sess_options=options, providers=["CPUExecutionProvider"])
        inputs = self.session.get_inputs()
        if len(inputs) != 1:
            raise ValueError("expected a single-input feed-forward policy (recurrent policies are not supported)")
        self.input_name = inputs[0].name
        width = inputs[0].shape[-1]
        if isinstance(width, int) and width != observation_dim:
            raise ValueError(f"policy expects {width} observations, deploy spec defines {observation_dim}")

    def __call__(self, observation: np.ndarray) -> np.ndarray:
        outputs = self.session.run(None, {self.input_name: observation.reshape(1, -1).astype(np.float32)})
        return np.asarray(outputs[0]).reshape(-1)
