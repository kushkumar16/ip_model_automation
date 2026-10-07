"""Cross-check the loop-aware approaches against hand-written timestamps.

loop_model_manual.py records every loop iteration with explicit `t0 = env.now` / record()
lines. Approaches A, E and F must produce exactly the same samples without them, and the
model must complete exactly the same work as the baseline.

The last section shows what is lost without the right boundary: the old per-job
behaviour (mode="job") never reports a loop, because a loop never returns, and the
default store_get boundary never reports loops that do not wait on a Store.

Run: python examples/soc_latency/run_loops.py        (reports only)
     python examples/soc_latency/run_loops.py -v     (also print every ERROR line)
"""

from __future__ import annotations

import logging
import random
import sys
from pathlib import Path

import loop_model
import loop_model_approach_a
import loop_model_approach_e
import loop_model_approach_f
import loop_model_manual
from latency_trace import TracedEnvironment

APPROACHES = [
    ("A: loops wrapped where they are started", loop_model_approach_a),
    ("E: decorator on each loop method", loop_model_approach_e),
    ("F: TracedEnvironment, no IP edits", loop_model_approach_f),
]
BUDGETS = Path(__file__).with_name("loop_budgets.yaml")


def without_boundaries(title: str, names: dict) -> None:
    random.seed(loop_model.SEED)
    env = TracedEnvironment(names=names, budgets=BUDGETS)
    loop_model.build(env)
    env.run(until=loop_model.SIM_TIME)
    env.monitor.report(title)
    if not env.monitor.samples:
        print("(no samples at all)")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "-v" not in sys.argv:
        logging.getLogger("latency").setLevel(logging.CRITICAL)

    base_blocks, _ = loop_model.run()
    expected = loop_model.completed(base_blocks)
    manual_blocks, manual = loop_model_manual.run()
    manual.report("Reference: hand-written timestamps in every loop")
    ok = loop_model.completed(manual_blocks) == expected

    for title, mod in APPROACHES:
        blocks, monitor = mod.run()
        monitor.report(title)
        if loop_model.completed(blocks) != expected:
            print(f"MISMATCH: {title} changed model behaviour")
            ok = False
        if monitor.samples != manual.samples:
            print(f"MISMATCH: {title} differs from the hand-written reference")
            ok = False

    print("\n---- without fsm_idle: loops that never wait on a Store are not reported ----")
    without_boundaries(
        "default store_get boundary only",
        {
            "WorkerIP.worker": "{self.name}.{pname}",
            "SchedulerIP.scheduler": "{self.name}.scheduler",
            "BridgeIP.bridge": "{self.name}.bridge",
        },
    )

    print("\nall loop approaches match the hand-written reference" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
