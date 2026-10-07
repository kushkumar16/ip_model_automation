"""Run all four latency-checker approaches on the same scenario and print each report.

Run: python examples/latency_checker/run_all.py          (reports only)
     python examples/latency_checker/run_all.py -v       (also print every ERROR line)
"""

from __future__ import annotations

import logging
import sys

import approach_a_wrapper
import approach_b_base_class
import approach_c_store_probe
import approach_d_txn_timestamp
from monitor import setup_logging

APPROACHES = [
    ("A: wrapper at registration", approach_a_wrapper),
    ("B: Pipeline base class", approach_b_base_class),
    ("C: Store probes", approach_c_store_probe),
    ("D: timestamp on the transaction", approach_d_txn_timestamp),
]

if __name__ == "__main__":
    setup_logging()
    if "-v" not in sys.argv:
        logging.getLogger("latency").setLevel(logging.CRITICAL)
    for title, mod in APPROACHES:
        mod.run().report(title)
