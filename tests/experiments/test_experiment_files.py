"""Every committed experiment file must validate, including plug-in params."""
from pathlib import Path

import pytest

from robot_controller import cli
from robot_controller.core.experiment import load

FILES = sorted((Path(__file__).resolve().parents[2] / "experiments").glob("*.yaml"))


def test_there_are_experiments():
    assert FILES


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_experiment_file_validates(path):
    assert cli.validate([str(path)]) == 0
    assert load(path).name == path.stem, "file name must match the experiment name (it becomes a label)"


def test_smoke_resume_keeps_the_contract_infra_relies_on():
    # infra_robot_sim recovery-test.yml runs the resume-smoke image with an external
    # fencing token and stops it with desired-state; it needs these properties.
    exp = load(Path(__file__).resolve().parents[2] / "experiments/smoke-resume.yaml")
    assert (exp.shards, exp.target_steps, exp.adapter.name, exp.limits.gpus_per_shard) == (1, 0, "smoke", 0)


def test_cli_reports_bad_files(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nname: bad\nadapter: {name: nope}\npolicy: {name: noop}\nlimits: {max_gpus: 1}\n")
    assert cli.validate([str(bad)]) == 1
    assert "unknown adapter" in capsys.readouterr().err
