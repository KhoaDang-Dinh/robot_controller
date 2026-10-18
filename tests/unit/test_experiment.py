import textwrap

import pytest

from robot_controller.core.experiment import ExperimentError, parse

BASE = """
schema_version: 1
name: exp-a
adapter: {name: smoke, params: {step_seconds: 0}}
policy: {name: noop}
limits: {max_gpus: 1}
"""


def exp(extra=""):
    return parse(BASE + textwrap.dedent(extra))


def test_defaults_and_hash_of_exact_bytes():
    a = exp()
    assert (a.shards, a.parallelism, a.target_steps, a.checkpoint_interval_seconds) == (1, 1, 0, 600)
    assert a.limits.gpus_per_shard == 1 and a.sync is None and not a.allow_config_change
    assert len(a.config_sha256) == 64
    # Any byte change (even a comment) is a different configuration on purpose.
    assert parse(BASE + "# note\n").config_sha256 != a.config_sha256


def test_seeds_single_or_per_shard():
    many = exp("shards: 3\nparallelism: 1\nseeds: [7, 8, 9]\n")
    assert [many.seed_for(i) for i in range(3)] == [7, 8, 9]
    one = exp("shards: 3\nparallelism: 1\nseeds: [100]\n")
    assert [one.seed_for(i) for i in range(3)] == [100, 101, 102]
    with pytest.raises(ExperimentError):
        exp("shards: 3\nparallelism: 1\nseeds: [1, 2]\n")


@pytest.mark.parametrize("extra, message", [
    ("unknown_key: 1\n", "unknown key"),
    ("shards: 0\n", "shards"),
    ("shards: 2\nparallelism: 3\n", "parallelism"),
    ("target_steps: -1\n", "target_steps"),
    ("checkpoint_interval_seconds: 0\n", "checkpoint_interval"),
    ("shards: 2\nparallelism: 1\nsync: {every_steps: 5}\n", "sync requires"),
    ("shards: 4\nparallelism: 4\nlimits: {max_gpus: 2}\n", "exceeds limits.max_gpus"),
    ("resume: {allow_config_change: yes, extra: 1}\n", "unknown key"),
])
def test_rejects_invalid(extra, message):
    text = BASE.replace("limits: {max_gpus: 1}\n", "") if "limits:" in extra else BASE
    with pytest.raises(ExperimentError, match=message):
        parse(text + extra)


@pytest.mark.parametrize("text, message", [
    ("schema_version: 2\nname: a\n", "schema_version"),
    ("schema_version: 1\nname: Bad_Name\n", "name"),
    ("- not a mapping\n", "mapping"),
    ("schema_version: 1\nname: a\nadapter: {name: smoke}\npolicy: {name: noop}\n", "limits"),
    ("schema_version: 1\nname: a\nadapter: {name: Smoke}\npolicy: {name: noop}\nlimits: {max_gpus: 0}\n", "identifier"),
    ("schema_version: true\nname: a\n", "boolean"),
])
def test_rejects_structure(text, message):
    with pytest.raises(ExperimentError, match=message):
        parse(text)
