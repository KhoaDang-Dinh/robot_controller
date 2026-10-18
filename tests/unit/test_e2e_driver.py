from e2e_kind_recovery import json_documents


def test_parses_back_to_back_kubectl_json_objects():
    text = '{\n "kind": "ServiceAccount"\n}\n{\n "kind": "Job"\n}\n'
    assert [d["kind"] for d in json_documents(text)] == ["ServiceAccount", "Job"]
    assert json_documents('{"kind": "List", "items": []}') == [{"kind": "List", "items": []}]
    assert json_documents("  \n") == []
