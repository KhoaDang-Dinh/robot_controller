import json

from usage_report import markdown, summarise, usage_records


def record(outcome, gpu_seconds, steps, capacity="spot", experiment="exp"):
    return json.dumps({"event": "usage", "outcome": outcome,
                       "usage": {"gpu_seconds": gpu_seconds, "wall_seconds": gpu_seconds,
                                 "steps": steps, "checkpoints": 1},
                       "resource": {"robot_sim.experiment": experiment, "robot_sim.job_id": "job",
                                    "robot_sim.capacity_type": capacity}})


def test_groups_usage_and_counts_wasted_attempts():
    lines = [
        "not json", '{"event": "checkpoint"}',
        "pod/x " + record("fenced", 1800, 10),
        record("completed", 5400, 90),
        record("completed", 3600, 50, capacity="on-demand"),
    ]
    rows = summarise(usage_records(lines), {"spot": 1.0})
    spot = next(r for r in rows if r["capacity_type"] == "spot")
    assert spot["attempts"] == 2 and spot["wasted_attempts"] == 1
    assert spot["gpu_hours"] == 2.0
    assert spot["wasted_gpu_seconds"] == 1800
    assert spot["estimated_usd"] == 2.0
    on_demand = next(r for r in rows if r["capacity_type"] == "on-demand")
    assert on_demand["estimated_usd"] is None  # no rate supplied: never guess a price
    assert "estimated_usd" in markdown(rows)


def test_no_rates_means_no_cost_column():
    rows = summarise(usage_records([record("completed", 60, 1)]))
    assert "estimated_usd" not in rows[0]
