import json
from pathlib import Path
import subprocess

import check_registry

ROOT = Path(__file__).resolve().parents[1]


def make_repo(tmp_path, files):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/images.json").write_text(json.dumps(
        {"registry": "ghcr.io/khoadang-dinh", "images": {"resume-smoke": {}}}))
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def test_repository_uses_one_registry():
    assert check_registry.check(ROOT) == []


def test_rejects_other_namespace_mixed_case_and_unknown_image(tmp_path):
    root = make_repo(tmp_path, {
        "a.yaml": "image: ghcr.io/other/resume-smoke@sha256:x\n",
        "b.yml": "IMAGE: ghcr.io/${{ github.repository_owner }}/resume-smoke\n",
        "c.md": "ghcr.io/khoadang-dinh/unknown-image\n",
        "ok.yml": "docker login ghcr.io\nimage: ghcr.io/khoadang-dinh/resume-smoke@sha256:x\n",
    })
    problems = "\n".join(check_registry.check(root))
    assert "a.yaml" in problems and "outside shared registry" in problems
    assert "b.yml" in problems and "repository_owner" in problems
    assert "c.md" in problems and "unknown-image" in problems
    assert "ok.yml" not in problems


def test_images_config_matches_infra_contract_shape():
    config = json.loads((ROOT / "config/images.json").read_text())
    assert config["registry"] == config["registry"].lower()
    for name, spec in config["images"].items():
        if spec["owner"] == "robot_controller":
            assert (ROOT / spec["dockerfile"]).is_file(), name
