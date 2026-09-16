---
name: systemc-model-generation
description: Generate or validate a SystemC (C++) transaction-level delay model and testbench for an IP that opts into the systemc backend, alongside its SimPy model. Use when the task mentions SystemC, SC_MODULE, SC_THREAD, TLM, sc_event, or a C++ delay model for a hardware IP. Opt-in per IP (ip.modeling_backends in the template must name systemc) — every other IP is untouched by this skill.
---

# SystemC Model Generation

A second modeling backend alongside SimPy, opt-in per IP. Most IPs never
touch this skill: SimPy is the default and the only backend an IP gets
unless its template explicitly asks for more.

## Opting an IP in

Add `modeling_backends` to the template's `ip:` block:

```yaml
ip:
  name: watchdog_ip
  ...
  modeling_backends: [simpy, systemc]
```

Omitting the field, or naming only `simpy`, is unaffected by this skill —
that is the default every IP already had before it existed, and stays that
way.

## Generating a scaffold

```shell
python tools/generate_systemc_scaffold.py templates/<ip>.template.yaml
```

Reads the same template `generate_model_scaffold.py` reads for SimPy
(reusing its helpers directly, so a state, an operation, or a command wait
point means the same thing in both scaffolds) and emits a `.h`/`.cpp` pair
into `systemc/models/` with `SC_THREAD` stubs per `fsm_processes` entry and
`TODO` markers for the behavior itself — exactly as unfinished as the SimPy
scaffold, and for the same reason: filling it in is implementation, not
generation. Refuses to run against a template that does not name `systemc`
in `ip.modeling_backends`.

## Implementing the model

Two sources of truth, deliberately:

- **The template** (`templates/<ip>.template.yaml`) is the contract both
  backends must satisfy — states, transitions, declared `latency_cycles`,
  wait models, commands.
- **The already-reviewed SimPy model** (`src/ip_model_automation/<ip>.py`),
  where one exists, is the reference for how the template's judgment calls
  were actually resolved — an edge case the template leaves implicit that
  the SimPy model already got right, gated by its own review history.
  Re-deriving those same judgment calls from scratch in a second language
  risks re-introducing a bug the SimPy model's own review rounds already
  found and fixed once.

Cycle counts translate as 1 declared cycle = 1 `SC_NS` — the same
dimensionless-cycle convention `env.timeout(N)` already uses on the SimPy
side. The template's `cycle_time_ns`/`clock_mhz` are a separate reporting
concern neither backend applies internally.

SystemC's concurrency primitives are not SimPy's, and a line-by-line port is
not the goal — a faithful port of the *behavior* is. Read
`references/systemc_idioms.md` before writing any command queue or racing a
`wait(sc_time, sc_event)` against a resource: it covers what has no direct
SystemC equivalent, the same same-instant-collision hazard
`simpy-model-generation`'s own reference documents (verified against real
SimPy source, but the underlying event-scheduling-order principle applies to
both languages), and the event-driven-vs-fixed-timestep abstraction choice
that decides which SystemC primitive (`SC_THREAD` vs `SC_METHOD`) fits.

## Layout

```text
systemc/
  models/<ip>.h        # SC_MODULE declaration
  models/<ip>.cpp       # process implementations
  tests/test_<ip>.cpp   # compiled testbench, one binary per IP
```

One flat header/source pair per IP, mirroring
`src/ip_model_automation/<ip>.py`'s own "one flat file per IP" convention —
not a per-IP folder. There is no `IP_ARTIFACTS`-style registry the way
`common.py` holds one for SimPy: `tools/run_systemc_tests.py`'s
`systemc_ips()` discovers opted-in IPs by reading every template's
`modeling_backends` directly, on every run, so it cannot drift from what the
templates actually declare.

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

## Rules

- Do not create a SystemC model for an IP whose template does not name
  `systemc` in `ip.modeling_backends`.
- Do not invent behavior the template doesn't state, even to fill a gap the
  SimPy port also had to resolve some other way — cite the same source
  (template or reviewed SimPy model) the SimPy port used.
- Keep the flat `systemc/models/<ip>.h`/`.cpp` layout; never a per-IP folder.
- Never leave scaffold `TODO` comments in a finalized model.

Read `references/systemc_idioms.md` for the full detail before writing any
model.
