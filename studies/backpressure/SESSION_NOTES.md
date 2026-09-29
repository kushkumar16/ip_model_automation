# Session notes: SimPy backpressure study

**Date:** 2026-09-29
**Branch:** `claude/backpressure-simulation-study-s6iowd`
**Folder:** `studies/backpressure/`
**Report (private artifact):** https://claude.ai/artifact/3vFck6dCzxQFDRxY2zM9XH

---

## 1. What was asked

1. Build a SimPy program to evaluate backpressure handling in a request/response process, and suggest which factors affect it. The starting list was:
   - FIFO size of request and response
   - latency of the response handler
   - issue rate of the request handler
2. Run iterations and prepare a report.
3. Share the files for download to a local machine.
4. Write this `.md` summary of the session.

## 2. What was built

### Model: `backpressure_sim.py`

```
Requester --> [req FIFO] --> Responder x N --> [resp FIFO] --> Response handler x M
    ^                                                                  |
    +------------- outstanding-credit return (max_outstanding) --------+
```

A full FIFO stalls the stage that feeds it, so backpressure travels right to left. A slow handler fills the response FIFO, which blocks the responder, which stops draining the request FIFO, which stalls the requester (or makes it drop requests).

The model sits outside `src/` on purpose. The models in `src/` are generated from templates and checked by the pipeline gates; this one is a standalone study.

**Settings.** Time is in cycles. Every field of `Config` can be passed with `--set` or `--sweep`.

| Factor | Field(s) |
|---|---|
| Issue rate | `issue_interval` |
| Issue burstiness | `issue_pattern` = const / poisson / burst, `burst_len`, `burst_gap` |
| FIFO sizes | `req_fifo_depth`, `resp_fifo_depth` |
| Responder latency / parallelism | `service_latency`, `service_dist`, `responder_workers` |
| Response handler latency / parallelism | `handler_latency`, `handler_dist`, `handler_workers` |
| Latency distributions | `const`, `exp`, `uniform` (±50%), `bimodal` (10% of requests take 5.5×, same mean) |
| Flow control | `flow_control` = ready / credit, `credit_return_latency` |
| Outstanding-request cap | `max_outstanding` (0 = unlimited) |
| Behavior when full | `on_full` = block / drop |

Four of these go beyond the original list: burstiness, latency variance, credit and flow-control delay, and the outstanding cap.

**Metrics reported:**
- throughput, and throughput as a fraction of the offered rate
- the bottleneck predicted by a simple capacity calculation (`static_bottleneck`)
- latency: mean, p50, p99 and max
- stall fraction for the requester and the responder
- average and peak occupancy of each FIFO, and the fraction of time it is full
- utilization of each stage, and the drop fraction

### Runner and report

- `run_study.py` runs 7 experiments: 177 configurations × 5 seeds, 100k cycles each with a 5k-cycle warm-up, in parallel. It writes `results/study_results.{json,csv}`, and with `--report` it also writes `results/report.html`.
- `report_template.html` is the report layout. The results JSON is embedded into it. It includes the charts (with hover values and data tables), a table of how the stall spreads upstream, a burst heatmap, and the sizing rules.

## 3. Checks against theory

These results matched what the model should produce:

- A handler slower than the issue interval (12 > 10 cycles) gave throughput = 1/12, with both FIFOs full.
- Credit flow control with depth 2 and a 32-cycle credit return gave throughput = 2/32.
- `max_outstanding=1` gave 1/(8+6) = 0.0714, one request per unloaded round trip.

## 4. Results

**Baseline:** issue 1 per 10 cycles, service 8, handler 6, FIFOs 4/4, one responder, one handler, ready flow control, block when full.

| # | Experiment | Key result |
|---|---|---|
| 1 | Offered load (Poisson issue; exponential service and handler) | Throughput is capped by the slowest stage. At 0.1 req/cycle, depth 2 delivered only 87%, because a blocked requester loses its issue slots. Depth 16 delivered 99.8%, but p99 latency went from 82 to 169 cycles. |
| 2 | Handler latency sweep | Constant handler: a cliff between 10 and 10.5 cycles. Median latency went from 18 to 105 cycles, both FIFOs filled, the responder stalled 24% of the time and the requester 5%. Exponential handler: gradual loss, already 1.7% at 90% handler utilization. |
| 3 | Response FIFO depth × handler jitter (mean 7) | Constant and uniform handlers needed depth 1. Exponential: responder stall went from 8.5% at depth 1 to 0% at depth 8. Bimodal needed depth 12–16 and lost 6% throughput at depth 1. |
| 4 | Burst length × request FIFO depth (drop policy) | At 80% average load, a 32-request burst still lost up to 81%. Losses stop at depth ≈ B − B·gap/service; for B=16, depth 12 lost 12.5% and depth 16 lost nothing. |
| 5 | Credit return latency × depth | Throughput = min(offered, depth / L) exactly. Depth 2 with L = 32 gave 62.5% of the offered rate. |
| 6 | Outstanding cap | The unloaded RTT (14 cycles) suggests a cap of 2, but 2 reached only 72%. It took 6–8 to match the uncapped result, because the loaded RTT is about 33 cycles. |
| 7 | Responder distribution, same mean | Depth 4: constant 97.7% vs bimodal 88.2%. Depth 16 recovers the throughput, but p99 latency reaches 200 cycles. |

### Sizing rules

| Item | Rule |
|---|---|
| Throughput ceiling | min(1/issue, N/service, M/handler, depth/L_credit, outstanding/RTT) |
| Request FIFO for bursts | ≥ B − B·gap/service_latency |
| Request FIFO with credits | ≥ issue_rate × credit round trip |
| Response FIFO | size for handler variance, not the mean: exp ≈ 4–8, heavy tail ≈ 12–16 at 70% load |
| Outstanding cap | ≥ issue_rate × loaded p90 RTT |
| Utilization | keep stages with variable latency ≤ about 85% |
| General | a deeper FIFO turns stalls and drops into queueing latency; watch p99, not just the mean |

## 5. Model limitations

- A blocked requester does not make up the slots it missed. It models a stalling source, not one with an unbounded queue.
- Latency is measured from when the requester tried to issue, so time the source spends stalled counts toward it.
- Responder workers can complete out of order. There is no reorder buffer and only one requester, so there is no arbitration.
- The ready signal is instant unless `flow_control=credit`. There is no almost-full threshold or skid buffer.

## 6. Files

| File | Purpose |
|---|---|
| `studies/backpressure/backpressure_sim.py` | SimPy model + CLI (`--set`, `--sweep`, `--csv`) |
| `studies/backpressure/run_study.py` | Experiment runner (`--seeds`, `--sim-time`, `--report`) |
| `studies/backpressure/report_template.html` | Report layout (data injected at build time) |
| `studies/backpressure/results/report.html` | Generated report (opens offline) |
| `studies/backpressure/results/study_results.csv` / `.json` | Averaged metrics with min/max across seeds |
| `studies/backpressure/README.md` | Usage, factor table, suggested experiments |
| `backpressure_study.zip` (repo root, not committed) | The whole folder zipped, for download |

## 7. How to run

```bash
pip install simpy
python studies/backpressure/backpressure_sim.py                            # one run, baseline
python studies/backpressure/backpressure_sim.py --set handler_latency=12   # override any field
python studies/backpressure/backpressure_sim.py \
    --sweep resp_fifo_depth=1,2,4,8,16 --sweep handler_dist=const,exp --csv sweep.csv
python studies/backpressure/run_study.py --report                          # full study, about 1 minute on 4 cores
```

## 8. Commits

- `ba0c60d`: Add standalone SimPy backpressure study for a request/response pipeline
- `292af8c`: Add backpressure experiment runner, results, and HTML report
- (this file): Add session notes for the backpressure study

## 9. Possible next steps

- Several requesters sharing the request FIFO through an arbiter (RR/WRR), to check fairness under backpressure.
- A reorder buffer so responses return in order when `responder_workers > 1`.
- An almost-full threshold with a registered ready signal and a skid buffer.
- Different request and response sizes (beats per transaction).
- A different clock rate for each stage (clock-domain crossing).
- Rerun the sweeps with the real latencies and FIFO sizes of a target IP from `templates/`.
