"""Cross-check every approach on the packet model (both process shapes).

Checks that:
- every approach sends exactly the same packets as the baseline;
- A, B, E, F and G record exactly the same handling times as the hand-written reference
  (packet_model_manual.py), for the per-packet process and for the `while True` loop;
- C and D record exactly the same end-to-end times as each other;
- for every packet, end-to-end = rx_q wait + handling (using G's rx_q wait), so the two
  measurements are consistent.

Run: python examples/soc_latency/run_packets.py        (reports only)
     python examples/soc_latency/run_packets.py -v     (also print every ERROR line)
"""

from __future__ import annotations

import logging
import math
import sys

import packet_model
import packet_model_approach_a
import packet_model_approach_b
import packet_model_approach_c
import packet_model_approach_d
import packet_model_approach_e
import packet_model_approach_f
import packet_model_approach_g
import packet_model_manual

HANDLING = [
    ("A: wrap where the process is started", packet_model_approach_a),
    ("B: subclasses override the IP methods", packet_model_approach_b),
    ("E: decorator on each method", packet_model_approach_e),
    ("F: TracedEnvironment, no IP edits", packet_model_approach_f),
    ("G: F + queue/resource waits", packet_model_approach_g),
]
END_TO_END = [
    ("C: probed rx/tx Stores", packet_model_approach_c),
    ("D: timestamp on the packet", packet_model_approach_d),
]
HANDLE_KEYS = ("par.handle", "ser.handle")


def sent(ips) -> dict:
    return {name: ip.sent for name, ip in ips.items()}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "-v" not in sys.argv:
        logging.getLogger("latency").setLevel(logging.CRITICAL)

    base_ips, _ = packet_model.run()
    expected = sent(base_ips)
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        if not cond:
            print("MISMATCH:", msg)
            ok = False

    manual_ips, manual = packet_model_manual.run()
    manual.report("Reference: hand-written timestamps (handling time)")
    check(sent(manual_ips) == expected, "manual reference changed model behaviour")

    results = {}
    for title, mod in HANDLING + END_TO_END:
        ips, monitor = mod.run()
        monitor.report(title)
        results[title[0]] = monitor.samples
        check(sent(ips) == expected, f"{title} changed model behaviour")

    for letter in "ABEFG":
        for key in HANDLE_KEYS:
            check(results[letter][key] == manual.samples[key], f"{letter} {key} differs from the reference")
    check(results["C"] == results["D"], "C and D end-to-end times differ")

    g = results["G"]
    for ip in ("par", "ser"):
        parts = [w + h for w, h in zip(g[f"{ip}.rx_q.wait"], g[f"{ip}.handle"], strict=True)]
        e2e = results["C"][f"{ip}.e2e"]
        check(all(math.isclose(a, b) for a, b in zip(parts, e2e, strict=True)), f"{ip}: e2e != rx_q wait + handling")

    print(
        "\nA/B/E/F/G match the hand-written reference, C == D, and e2e = rx_q wait + handling for every packet"
        if ok
        else "\nFAILED"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
