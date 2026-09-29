#!/usr/bin/env python3
"""Run the backpressure experiment set and write the results (and the HTML report).

Each experiment sweeps one or two knobs around a baseline, runs every point with
several seeds, and averages the metrics. Results go to ``results/study_results.json``
and ``results/study_results.csv``; with ``--report`` the JSON is also embedded into
``report_template.html`` to produce ``results/report.html``.

Usage::

    python studies/backpressure/run_study.py --report
    python studies/backpressure/run_study.py --seeds 3 --sim-time 50000   # quicker
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backpressure_sim import Config, simulate  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "results"

# Baseline: responder capacity 1/8, handler capacity 1/6, offered 1/10 (responder 80% busy).
BASE = Config(issue_interval=10.0, service_latency=8.0, handler_latency=6.0, req_fifo_depth=4, resp_fifo_depth=4)

EXPERIMENTS: List[Dict] = [
    {
        "id": "load",
        "title": "Offered load vs throughput and latency",
        "question": "Where does the pipeline saturate, and what happens to latency on the way there?",
        "base": {"issue_pattern": "poisson", "service_dist": "exp", "handler_dist": "exp"},
        "x": ("issue_interval", [40, 20, 16, 13, 11.5, 10.5, 10, 9.5, 9, 8.5, 8, 7, 6]),
        "series": ("req_fifo_depth", [2, 4, 16]),
        "series_set": "resp_fifo_depth",
    },
    {
        "id": "resp_depth",
        "title": "Response FIFO depth vs handler jitter",
        "question": "How deep must the response FIFO be to keep a jittery handler from stalling the responder?",
        "base": {"handler_latency": 7.0},
        "x": ("resp_fifo_depth", [1, 2, 3, 4, 6, 8, 12, 16, 32]),
        "series": ("handler_dist", ["const", "uniform", "exp", "bimodal"]),
    },
    {
        "id": "handler",
        "title": "Handler latency and upstream propagation",
        "question": "When the response handler is the slowest stage, how far back does the stall travel?",
        "base": {},
        "x": ("handler_latency", [4, 6, 8, 9, 10, 10.5, 11, 12, 14, 16]),
        "series": ("handler_dist", ["const", "exp"]),
    },
    {
        "id": "burst",
        "title": "Burst absorption (drop policy)",
        "question": "How much request FIFO does a bursty source need before it stops losing requests?",
        "base": {"issue_pattern": "burst", "on_full": "drop", "burst_gap": 1.0},
        "x": ("req_fifo_depth", [2, 4, 8, 12, 16, 24, 32]),
        "series": ("burst_len", [4, 8, 16, 32]),
    },
    {
        "id": "credit",
        "title": "Credit loop latency vs request FIFO depth",
        "question": "How does a slow credit return limit throughput for each FIFO depth?",
        "base": {"flow_control": "credit"},
        "x": ("credit_return_latency", [0, 4, 8, 16, 24, 32, 48, 64]),
        "series": ("req_fifo_depth", [2, 4, 8, 16]),
    },
    {
        "id": "outstanding",
        "title": "Outstanding-request cap",
        "question": "How many requests must be allowed in flight to reach full rate?",
        "base": {"issue_pattern": "poisson", "service_dist": "exp", "handler_dist": "exp"},
        "x": ("max_outstanding", [1, 2, 3, 4, 6, 8, 12]),
        "series": ("issue_interval", [10.0, 8.5]),
    },
    {
        "id": "tail",
        "title": "Responder latency distribution (same mean)",
        "question": "With the mean fixed, what does latency spread alone cost?",
        "base": {"issue_pattern": "poisson"},
        "x": ("service_dist", ["const", "uniform", "exp", "bimodal"]),
        "series": ("resp_fifo_depth", [4, 16]),
        "series_set": "req_fifo_depth",
    },
]

NUMERIC_METRICS = [
    "throughput",
    "achieved_vs_offered",
    "lat_mean",
    "lat_p50",
    "lat_p99",
    "req_stall_frac",
    "resp_stall_frac",
    "responder_util",
    "handler_util",
    "req_fifo_avg",
    "resp_fifo_avg",
    "req_fifo_full_frac",
    "resp_fifo_full_frac",
    "drop_frac",
]


def _points(exp: Dict, base: Config, seeds: int):
    xname, xvals = exp["x"]
    sname, svals = exp["series"]
    for s, x, seed in itertools.product(svals, xvals, range(1, seeds + 1)):
        overrides = {**exp["base"], xname: x, sname: s, "seed": seed}
        if exp.get("series_set"):  # the series value also drives a second field (e.g. both FIFO depths)
            overrides[exp["series_set"]] = s
        yield exp["id"], s, x, replace(base, **overrides)


def _run(job):
    exp_id, s, x, cfg = job
    return exp_id, s, x, simulate(cfg)


def run_all(seeds: int, sim_time: float, workers: int) -> Dict:
    base = replace(BASE, sim_time=sim_time)
    jobs = [j for exp in EXPERIMENTS for j in _points(exp, base, seeds)]
    raw: Dict = {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for exp_id, s, x, res in pool.map(_run, jobs, chunksize=4):
            raw.setdefault((exp_id, s, x), []).append(res)

    out = {"baseline": asdict(base), "seeds": seeds, "experiments": []}
    for exp in EXPERIMENTS:
        rows = []
        for s in exp["series"][1]:
            for x in exp["x"][1]:
                runs = raw[(exp["id"], s, x)]
                row = {"series": s, "x": x, "static_bottleneck": runs[0]["static_bottleneck"]}
                for m in NUMERIC_METRICS:
                    vals = [r[m] for r in runs]
                    row[m] = statistics.fmean(vals)
                    row[m + "_min"] = min(vals)
                    row[m + "_max"] = max(vals)
                rows.append(row)
        out["experiments"].append(
            {
                "id": exp["id"],
                "title": exp["title"],
                "question": exp["question"],
                "overrides": exp["base"],
                "x_name": exp["x"][0],
                "series_name": exp["series"][0],
                "rows": rows,
            }
        )
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--sim-time", type=float, default=100_000.0)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--report", action="store_true", help="also render results/report.html")
    args = parser.parse_args(argv)

    data = run_all(args.seeds, args.sim_time, args.workers)
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "study_results.json").write_text(json.dumps(data, indent=1))
    with open(OUT_DIR / "study_results.csv", "w", newline="") as fh:
        writer = None
        for exp in data["experiments"]:
            for row in exp["rows"]:
                flat = {"experiment": exp["id"], "x_name": exp["x_name"], "series_name": exp["series_name"], **row}
                if writer is None:
                    writer = csv.DictWriter(fh, fieldnames=list(flat.keys()))
                    writer.writeheader()
                writer.writerow(flat)
    n = sum(len(e["rows"]) for e in data["experiments"])
    print(f"{n} points x {args.seeds} seeds -> {OUT_DIR / 'study_results.json'}")

    if args.report:
        template = (HERE / "report_template.html").read_text()
        html = template.replace("/*__STUDY_DATA__*/null", json.dumps(data, separators=(",", ":")))
        (OUT_DIR / "report.html").write_text(html)
        print(f"report -> {OUT_DIR / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
