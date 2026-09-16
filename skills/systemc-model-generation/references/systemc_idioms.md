# SystemC Idioms

Concrete idioms for this backend, drawn from real open-source SystemC code
(`agra-uni-bremen/riscv-vp`, `mariusmm/RISC-V-TLM`) and this project's own
`watchdog_ip` port — not generic SystemC tutorial content.

## SimPy's Store.get()/.put() has no direct equivalent

The `watchdog_ip` port uses a plain `std::deque` guarded by an `sc_event`,
with the consumer checking the queue's actual state before every `wait()`
rather than trusting only the event's edge-triggered fire — see the comment
above `run_armed()` in `systemc/models/watchdog_ip.cpp` for why that
distinction matters once more than one command can be in flight, and why it
doesn't change anything observable for an IP whose interface itself is
documented as "one command accepted at a time."

`wait(sc_time, sc_event)` is SystemC's direct counterpart to SimPy's
`yield event_a | event_b` race — but check what actually happened (here, the
shared queue's state) after `wait()` returns, not `sc_event::triggered()`
alone; the latter is correct but brittle across delta-cycle boundaries in a
way checking real state is not.

## The same-instant collision hazard applies here too

Every command-queue race this backend writes should follow
`../../simpy-model-generation/references/simpy_concurrency_patterns.md`'s
same-instant-collision principle, even though that doc is written against
SimPy's API: the underlying hazard (a freshly-built wait can technically win
and still be silently lost if it is discarded rather than re-checked) is
about *event scheduling order*, not about either library specifically.
`run_armed()`'s check-the-real-queue-state pattern (above) is this project's
SystemC-side answer to it.

## Choosing event-driven vs fixed-timestep

Real open-source SystemC models split cleanly into two abstraction styles,
and picking the wrong one for an IP peripheral costs real simulation
performance for no accuracy gain:

- **Event-driven** (what every model in this repo uses): a process computes
  the exact delay until the next thing that matters and `wait()`s exactly
  that long — `wait(latency["arm"])`, not "wait one cycle and check again".
  The process is asleep, consuming zero simulation steps, for however many
  cycles nothing relevant happens. This matches `templates/*.template.yaml`'s
  own `abstraction_level: transaction` declaration and SimPy's own idiom on
  the other backend (`env.timeout(self.latency[...])`), not a cycle counter.
- **Fixed-timestep** (cycle-accurate CPU/core models, not appropriate here):
  a process re-runs every single clock cycle regardless of activity —
  `mariusmm/RISC-V-TLM`'s CPU model (`src/CPU.cpp`) is a real, representative
  example: `while (true) { CPU_step(); wait(default_time); }`, an
  unconditional per-cycle `wait()` because instruction fetch/execute
  genuinely has to happen every cycle. A peripheral that is idle almost all
  the time (a watchdog counting down over hundreds of cycles, a UART between
  characters) gains nothing from paying that per-cycle cost and should not
  copy this shape.

If a future IP's DLD ever calls for genuinely cycle-by-cycle behavior (a
combinational block whose output must be recomputed every clock edge), that
is exactly what `SC_METHOD` with a clock-edge sensitivity list
(`sensitive << clk.pos()`) is for — a different SystemC primitive than the
`SC_THREAD` + `wait(latency)` style every model here uses, and worth reaching
for deliberately rather than reproducing a `while(true){...; wait(1, SC_NS);}`
polling loop with `SC_THREAD`.

## Advanced: bridging external I/O (not needed for a typical IP peripheral)

An IP that talks to something outside the simulation itself (a real file
descriptor, a host socket) needs to get a notification from a non-SystemC
thread into the SystemC kernel, and `sc_event::notify()` is not safe to call
from outside the kernel's own thread. `agra-uni-bremen/riscv-vp`'s UART model
(`vp/src/platform/common/fd_abstract_uart.cpp`) is real, working prior art
for this: a plain `std::thread` polls the file descriptor and calls
`asyncEvent.notify()` (a thread-safe wrapper SystemC's `async_event`
extension provides) to wake the SC_THREAD side. None of this repo's IPs need
it — every one models an internal digital block, not a host-facing device —
but it is the reference to reach for if that ever changes, rather than
inventing a bridging mechanism from scratch.
