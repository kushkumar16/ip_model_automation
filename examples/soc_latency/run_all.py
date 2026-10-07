"""Run the baseline and every approach (A-G), print each report, and cross-check them.

Checks that:
- every approach completes exactly the same transactions as the baseline (the checker
  does not change model behaviour), and
- every approach measures identical per-pipeline latencies (stage and wait rows excluded).

Run: python examples/soc_latency/run_all.py        (reports only)
     python examples/soc_latency/run_all.py -v     (also print every ERROR line)
"""

from __future__ import annotations

import logging
import sys

import soc_model
import soc_model_approach_a
import soc_model_approach_b
import soc_model_approach_c
import soc_model_approach_d
import soc_model_approach_e
import soc_model_approach_f
import soc_model_approach_g

APPROACHES = [
    ("A: wrap the handler at registration", soc_model_approach_a),
    ("B: mixin overrides the IP's hooks", soc_model_approach_b),
    ("C: probed input/output Stores", soc_model_approach_c),
    ("D: timestamp on the transaction", soc_model_approach_d),
    ("E: @latency_traced decorator", soc_model_approach_e),
    ("F: TracedEnvironment, no IP edits", soc_model_approach_f),
    ("G: F + TracedResource/TracedStore waits", soc_model_approach_g),
]


def completed(blocks):
    return {ip: dict(sorted(b.completed.items())) for ip, b in blocks.items()}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "-v" not in sys.argv:
        logging.getLogger("latency").setLevel(logging.CRITICAL)

    base_blocks, _ = soc_model.run()
    expected = completed(base_blocks)
    reference = None
    ok = True
    for title, mod in APPROACHES:
        blocks, monitor = mod.run()
        monitor.report(title)
        if completed(blocks) != expected:
            print(f"MISMATCH: {title} changed model behaviour")
            ok = False
        pipelines = {k: v for k, v in monitor.samples.items() if k.count(".") == 1}  # skip stage keys
        if reference is None:
            reference = pipelines
        elif pipelines != reference:
            print(f"MISMATCH: {title} measured different latencies")
            ok = False
    print("\nall approaches agree with each other and with the baseline" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
