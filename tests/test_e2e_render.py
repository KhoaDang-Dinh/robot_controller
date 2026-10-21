import json

import pytest

import e2e_kind_recovery as recovery


@pytest.mark.parametrize("as_list", [False, True])
def test_render_handles_kubectl_multi_document_output(monkeypatch, as_list):
    account = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "resume-smoke"}}
    job = {"kind": "Job", "spec": {"template": {"spec": {
        "imagePullSecrets": [{"name": "ghcr-pull"}],
        "containers": [{"image": "old", "env": []}]
    }}}}
    objects = [account, job]
    output = json.dumps({"kind": "List", "items": objects}) if as_list else "\n".join(map(json.dumps, objects))
    monkeypatch.setattr(recovery, "sh", lambda *args, **kwargs: output)
    result = json.loads(recovery.render(1, 1, "test", "local:image", "http://emulator:5000"))
    assert result["items"][0] == account
    pod = result["items"][1]["spec"]["template"]["spec"]
    assert "imagePullSecrets" not in pod
    assert pod["containers"][0]["image"] == "local:image"
    assert pod["containers"][0]["imagePullPolicy"] == "Never"
