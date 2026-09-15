# SystemC Backend

A second modeling backend alongside SimPy, opt-in per IP. Most IPs in this
repo never touch this directory: SimPy remains the default and the only
backend an IP gets unless its template explicitly asks for more.

## Opting an IP in

Add `modeling_backends` to the template's `ip:` block:

```yaml
ip:
  name: watchdog_ip
  ...
  modeling_backends: [simpy, systemc]
```

Omitting the field, or naming only `simpy`, is unaffected by anything in this
directory -- that is the default every existing IP already had before this
backend existed, and stays that way.

## Layout

```text
systemc/
  models/<ip>.h        # SC_MODULE declaration
  models/<ip>.cpp       # process implementations
  tests/test_<ip>.cpp   # compiled testbench, one binary per IP
```

One flat header/source pair per IP, mirroring `src/ip_model_automation/<ip>.py`'s
own "one flat file per IP" convention -- not a per-IP folder.

## Generating a scaffold

```shell
python tools/generate_systemc_scaffold.py templates/<ip>.template.yaml
```

Reads the same template `generate_model_scaffold.py` reads for SimPy (reusing
its helpers directly, so a state, an operation, or a command wait point means
the same thing in both scaffolds) and emits a `.h`/`.cpp` pair into
`systemc/models/` with `SC_THREAD` stubs per `fsm_processes` entry and `TODO`
markers for the behavior itself -- exactly as unfinished as the SimPy
scaffold is, and for the same reason: filling it in is implementation, not
generation.

Refuses to run against a template that does not name `systemc` in
`ip.modeling_backends`.

## Implementing the model

Two sources of truth, deliberately:

- **The template** (`templates/<ip>.template.yaml`) is the contract both
  backends must satisfy -- states, transitions, declared `latency_cycles`,
  wait models, commands.
- **The already-reviewed SimPy model** (`src/ip_model_automation/<ip>.py`),
  where one exists, is the reference for how the template's judgment calls
  were actually resolved -- an edge case the template leaves implicit (a
  command with no declared transition from some state, a dual wait point a
  reviewer found and the template was corrected to state) that the SimPy
  model already got right, gated by its own review history. Re-deriving those
  same judgment calls from scratch in a second language risks re-introducing
  a bug the SimPy model's own review rounds already found and fixed once.

Cycle counts translate as 1 declared cycle = 1 `SC_NS` -- the same
dimensionless-cycle convention `env.timeout(N)` already uses on the SimPy
side. The template's `cycle_time_ns`/`clock_mhz` are a separate reporting
concern neither backend applies internally.

SystemC's concurrency primitives are not SimPy's, and a line-by-line port is
not the goal -- a faithful port of the *behavior* is. In particular:

- SimPy's `Store.get()`/`.put()` has no direct SystemC equivalent. The
  `watchdog_ip` port uses a plain `std::deque` guarded by an `sc_event`, with
  the consumer checking the queue's actual state before every `wait()` rather
  than trusting only the event's edge-triggered fire -- see the comment above
  `run_armed()` in `systemc/models/watchdog_ip.cpp` for why that distinction
  matters once more than one command can be in flight, and why it doesn't
  change anything observable for an IP whose interface itself is documented
  as "one command accepted at a time."
- `wait(sc_time, sc_event)` is SystemC's direct counterpart to SimPy's
  `yield event_a | event_b` race -- but check what actually happened (here,
  the shared queue's state) after `wait()` returns, not `sc_event::triggered()`
  alone; the latter is correct but brittle across delta-cycle boundaries in a
  way checking real state is not.
- Every command-queue race this backend writes should follow
  `skills/ip-model-generation/references/simpy_concurrency_patterns.md`'s
  same-instant-collision principle even though that doc is written against
  SimPy's API: the underlying hazard (a freshly-built wait can technically
  win and still be silently lost if it is discarded rather than re-checked)
  is about *event scheduling order*, not about either library specifically.
  `run_armed()`'s check-the-real-queue-state pattern above is this project's
  SystemC-side answer to it.

## Choosing event-driven vs fixed-timestep

Real open-source SystemC models split cleanly into two abstraction styles,
and picking the wrong one for an IP peripheral costs real simulation
performance for no accuracy gain:

- **Event-driven** (what every model in this repo uses): a process computes
  the exact delay until the next thing that matters and `wait()`s exactly
  that long -- `wait(latency["arm"])`, not "wait one cycle and check again".
  The process is asleep, consuming zero simulation steps, for however many
  cycles nothing relevant happens. This matches `templates/*.template.yaml`'s
  own `abstraction_level: transaction` declaration and SimPy's own idiom on
  the other backend (`env.timeout(self.latency[...])`), not a cycle counter.
- **Fixed-timestep** (cycle-accurate CPU/core models, not appropriate here):
  a process re-runs every single clock cycle regardless of activity --
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
(`sensitive << clk.pos()`) is for -- a different SystemC primitive than the
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
it -- every one models an internal digital block, not a host-facing device --
but it is the reference to reach for if that ever changes, rather than
inventing a bridging mechanism from scratch.

## Running the tests

```shell
python tools/run_systemc_tests.py            # every IP opted into systemc
python tools/run_systemc_tests.py watchdog_ip # one IP
```

Compiles each opted-in IP's `systemc/models/<ip>.{h,cpp}` against
`systemc/tests/test_<ip>.cpp` with g++ and the system SystemC library
(`apt-get install libsystemc-dev`; `pkg-config systemc` reports whether it is
installed), then runs the resulting binary for real. Exit code and per-IP
pass/fail mirror `python -m unittest tests.test_<ip>` on the SimPy side.

A SystemC testbench is one compiled binary per IP with one `sc_main`, not one
binary per scenario: SystemC's kernel elaborates once per process, so each
scenario gets its own model instance and driver `SC_THREAD` inside that one
`sc_main`, all running concurrently within one shared simulated timeline (see
`systemc/tests/test_watchdog_ip.cpp`).

## Registry

There is no `IP_ARTIFACTS`-style registry for SystemC the way `common.py`
holds one for SimPy -- `tools/run_systemc_tests.py`'s `systemc_ips()`
discovers opted-in IPs by reading every template's `modeling_backends`
directly, on every run, so it can never drift from what the templates
actually declare.
