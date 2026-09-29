# Backpressure study (SimPy)

A standalone SimPy model for evaluating how backpressure behaves in a
request/response process. It is a study tool, not one of the template-driven IP
models under `src/`, so it lives outside the pipeline gates.

```
Requester --> [req FIFO] --> Responder x N --> [resp FIFO] --> Response handler x M
    ^                                                                  |
    +------------- outstanding-credit return (max_outstanding) --------+
```

A slow response handler fills the response FIFO, which stalls the responder,
which stops draining the request FIFO, which stalls (or drops at) the
requester. The model reports where time is lost at each stage.

## Running

```bash
python studies/backpressure/backpressure_sim.py                           # one run, defaults
python studies/backpressure/backpressure_sim.py --set handler_latency=12  # override any field
python studies/backpressure/backpressure_sim.py \
    --sweep resp_fifo_depth=1,2,4,8,16 --sweep handler_dist=const,exp --csv sweep.csv
```

Every field of `Config` can be passed to `--set` or `--sweep`. Several
`--sweep` flags run the full grid of combinations. Time is in cycles.

## What affects backpressure (and which knob models it)

| Factor | Knob | What to look for |
| --- | --- | --- |
| Issue rate | `issue_interval` | Throughput tops out at `min(1/issue_interval, responder_workers/service_latency, handler_workers/handler_latency)`. Once the issue rate is higher than that, `req_stall_frac` rises and the FIFOs sit full. |
| Burstiness of issue | `issue_pattern` = const / poisson / burst, `burst_len`, `burst_gap` | Bursts need FIFO depth even when the average rate is fine. Size the req FIFO for roughly `burst_len × (1 − service_rate/burst_rate)`. |
| Request FIFO depth | `req_fifo_depth` | Absorbs issue jitter and bursts. It cannot fix a sustained rate mismatch: extra depth past that point only adds latency. |
| Response FIFO depth | `resp_fifo_depth` | Decouples the responder from handler jitter. `resp_stall_frac` shows how often the responder is blocked by a full response FIFO. |
| Response handler latency | `handler_latency`, `handler_dist`, `handler_workers` | When it is the slowest stage, backpressure travels all the way back to the requester. Variance matters as well as the mean: compare `const` with `exp` or `bimodal`. |
| Responder latency / parallelism | `service_latency`, `service_dist`, `responder_workers` | Pipelining (`responder_workers`) multiplies capacity. Tail latency (`bimodal`) causes head-of-line stalls. |
| Flow-control signalling delay | `flow_control=credit`, `credit_return_latency` | With credits, throughput ≤ `req_fifo_depth / credit_return_latency`. A shallow FIFO combined with a long credit loop limits throughput even when every stage is fast. |
| Outstanding-transaction cap | `max_outstanding` | Little's law: throughput ≤ `max_outstanding / round_trip_latency`. |
| Overflow policy | `on_full` = block / drop | Blocking gives lossless operation and moves the stall upstream. Dropping keeps the source running and loses requests (`drop_frac`). |

## Metrics

- `throughput`, `achieved_vs_offered`: completed requests per cycle, and that rate as a fraction of the offered rate.
- `static_bottleneck`: the stage with the lowest capacity according to the analytic estimate. If the simulated result disagrees with it, variance or FIFO sizing is the cause.
- `lat_mean / p50 / p99 / max`: end-to-end latency, measured from when the request wanted to issue (source stall included) to when the handler finishes.
- `req_stall_frac`: fraction of time the requester is blocked (FIFO full, no credit, or outstanding cap reached).
- `resp_stall_frac`: fraction of responder-worker time spent blocked on a full response FIFO.
- `*_fifo_avg / max / full_frac`: time-weighted FIFO occupancy, peak occupancy, and fraction of time the FIFO is full.
- `responder_util`, `handler_util`, `drop_frac`.

## Suggested experiments

1. **Find the knee.** Sweep `issue_interval` from low load to overload and plot throughput and p99 latency. Latency rises steeply as utilization approaches 1.
2. **FIFO sizing under jitter.** Set `handler_dist=exp` and sweep `resp_fifo_depth`. Find the smallest depth where `resp_stall_frac` is close to 0.
3. **Bursts.** Set `issue_pattern=burst` and sweep `burst_len` × `req_fifo_depth`, with `on_full=drop` and then `block`.
4. **Credit loop.** Set `flow_control=credit` and sweep `credit_return_latency` × `req_fifo_depth`. Check that throughput follows `depth / latency`.
5. **Slow consumer.** Set `handler_latency` above `service_latency` and above `issue_interval`, then watch both FIFOs fill and the stall move upstream.
6. **Tail latency.** Compare `service_dist=const` with `bimodal` at the same mean.

## Possible extensions

- Multiple requesters with an arbiter (RR/WRR) sharing one request FIFO, to study fairness under backpressure.
- In-order responses (a reorder buffer) when `responder_workers > 1`.
- An almost-full threshold with a registered ready signal (skid buffer) in place of instant ready.
- Different request and response sizes (bytes per beat) on each link.
- Clock-domain crossing: a different clock rate for each stage.
