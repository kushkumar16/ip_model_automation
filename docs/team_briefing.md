# IP DLD → Model Automation — Team Briefing / Reference

A presenter's reference for explaining this system to the team. Everything here
is drawn from the repo as it stands today; every file path is real and can be
opened live during the talk.

- Canonical deep doc: [project_overview.md](project_overview.md) (this briefing is the *talk track* over it)
- Command reference: [README.md](../README.md)
- Diagrams to drop into slides: [diagrams/](diagrams/) (`index.html` opens them all)

**Current state (verified while writing this):** 9 IPs + 2 subsystems modeled,
11 templates promoted, 117 unit tests passing, wait-model coverage OK for all 11
models, code coverage gate at 95% total *and* per file.

---

## 1. The 60-second pitch

> An engineer writes a DLD for an IP block, drops it in a folder, and runs one
> command. Out comes a runnable SimPy performance model of that IP, a unit-test
> suite that proves it, and a report of everything the DLD forgot to say.
> Nothing is ever invented — anything the DLD doesn't state is flagged, not
> guessed, and automated gates block progress until it's resolved.

Three things to stress, because they are the parts people don't expect:

1. **It's not "LLM writes a model."** A normalized YAML *template* sits between
   the DLD and the model, and machine-checkable gates guard every hop. The LLM
   (or a human) only fills two judgment steps, and the same gates judge its
   output either way.
2. **Missing information is a first-class output.** The gaps report is arguably
   more valuable than the model on day one — it tells the DLD author exactly
   what they left ambiguous, before RTL exists.
3. **Editing a DLD amends the model, it doesn't regenerate it.** The template
   diff gives a typed, blast-radius-tagged change list that maps to edit sites.

---

## 2. Why we want this at all

Before RTL exists, architecture questions still need answers:

- What's the end-to-end latency of a DMA descriptor under load?
- Where does backpressure build up when the completion queue is slow?
- Is the arbitration policy fair across tenants at high occupancy?

Today those answers come from a spreadsheet or from a hand-written model that
one person maintains and nobody trusts after the spec changes. The gap is never
the SimPy coding — it's *keeping the model honest against the document*, and
knowing what the document never said.

This system attacks exactly those two problems:

| Problem | What the system does |
| --- | --- |
| Model drifts from spec | The model is generated from a reviewed template, and provenance stamps detect drift |
| Spec is ambiguous / incomplete | Gaps report + `TODO_REVIEW` markers; gates refuse to promote a template that still has one |
| Each DLD is written differently | Extractor normalizes any DLD shape into one fixed template schema |
| "Is the model right?" | Six gates, one command, one green/red answer |

---

## 3. The one idea the whole system is built on

**Two sources of truth — but only one at a time.**

```text
Phase A (authoring):    DLD  ──►  Template        the DLD is the source
Phase B (generation):   Template ──►  Model + Tests   the template is the source
```

Once the template is reviewed and promoted, **the DLD is never read again**.
That is what lets a reviewer sign off *once*, on one artifact, and trust
everything downstream — model, tests, docs, experiments.

Diagram to show: [diagrams/03_two_sources_of_truth.svg](diagrams/03_two_sources_of_truth.svg)

Why not go DLD → model directly? Because prose is inconsistent and incomplete
by nature. You cannot diff it usefully, you cannot lint it, and you cannot prove
a model covers it. A normalized YAML in the middle makes all three mechanical.

**Honest caveat to state out loud** (someone will ask): *structure* is provably
faithful end to end — an FSM, queue, resource, or scenario mismatch between
template and model is a gate failure. *Timing numbers* are a coherent, reviewed
reference (the linter checks `ns == cycles × cycle_time_ns`), but they are not
asserted equal to the model's constructor defaults — models default latencies
low so unit tests run in a few simulated ticks and take the template's values as
overrides.

---

## 4. The pipeline, stage by stage

```text
DLD markdown (or .docx)
  ─► dld_to_template.py        draft template + gaps report
  ─► review                    resolve every TODO_REVIEW (DLD-stated behavior only)
  ─► gates                     coverage --strict + lint  →  promote to golden template
  ─► fork:  new IP   → scaffold + prompt pack + implement
            existing → template diff + amend in place
  ─► unit tests, provenance stamp, repo-wide gates
```

Diagram: [diagrams/02_end_to_end_flow.svg](diagrams/02_end_to_end_flow.svg)

### Stage 0 — extraction

```bash
python tools/dld_to_template.py dlds/mailbox_ip_dld.md
```

The extractor is a best-effort parser, **not an oracle**. It reliably picks up
the mechanical facts and deliberately refuses the judgment calls:

| Extracted automatically | Left for review (flagged `TODO_REVIEW`) |
| --- | --- |
| FSM names, their states, declared FSM count | Command encodings |
| Interface sections | Test scenarios |
| Per-interface wait models | Invariants, APIs, state variables |
| Per-FSM timing operations, clock rate | Transition detail |

Why that split: those four are where DLDs are most often ambiguous, so guessing
there is where a wrong model would come from.

**How we know the extractor can be trusted** — it is calibrated against every
existing golden IP: re-parsing each DLD must reproduce its template's FSM name
set exactly, and the DLD's stated FSM count must match the template's
`fsm_count`. That equality runs as a regression test. (Interface matching is
report-only, because templates legitimately consolidate several DLD interface
sections; FSM coverage is the hard gate.)

### Stage 1 — review

A human or an LLM under contract replaces every `TODO_REVIEW`, using **only**
DLD-stated behavior. The rule to repeat in the meeting: *if the DLD is silent,
fix the DLD — don't invent.*

### Stage 2 — gates and promotion

```bash
python tools/check_template_coverage.py templates/mailbox_ip.template.draft.yaml dlds/mailbox_ip_dld.md --strict
python tools/template_lint.py templates/mailbox_ip.template.yaml
```

### Stage 3 — model + tests

```bash
python tools/generate_model_scaffold.py templates/mailbox_ip.template.yaml --output-dir src/ip_model_automation
python tools/generate_prompt_pack.py templates/mailbox_ip.template.yaml --output-dir prompt_packs
```

The scaffold emits one model class and one SimPy process method per template
FSM. The prompt pack bundles template + contract + skill instructions into a
self-contained prompt, so an implementer (human or LLM) needs nothing else —
notably *not* the DLD.

---

## 5. Worked example — `mailbox_ip`, document to test

This is the demo to run live. Every artifact below exists in the repo.

### 5.1 What the DLD says

[dlds/mailbox_ip_dld.md](../dlds/mailbox_ip_dld.md) §6.2:

```text
### 6.2 Message Push FSM
Role: Accept sender messages and enqueue them into the channel FIFO.
States:
- IDLE
- ACCEPT_MESSAGE
- CHECK_SPACE
- ENQUEUE
- REJECT_FULL

| FSM/process      | Runs as                      | Delay model                       |
| Message Push FSM | Parallel per-channel process | Accept message: 1 cycle = 2 ns. … |
```

…and §12, honestly, what the author didn't know yet: number of channels,
message width, FIFO depth per channel, interrupt target routing, whether an
overflow raises an error interrupt.

### 5.2 What extraction produced

```text
wrote draft:  templates\mailbox_ip.template.draft.yaml
wrote report: reports\mailbox_ip.gaps.md
wrote doc:    reports\template_docs\mailbox_ip.template.draft.html
```

The gaps report (real output):

```text
## Extracted
- FSMs (5): register_access, message_push, message_pop, doorbell, interrupt_notify
- Interfaces (4): register_if, sender_message_if, receiver_message_if, interrupt_output_if
- Declared fsm_count: 5

## Missing / To Review
- commands: not derivable from DLD; TODO_REVIEW placeholder emitted
- test_scenarios: author from DLD behavior; TODO_REVIEW placeholder emitted
- functionality_model.invariants/apis/state_variables: complete from DLD

## DLD Open Items
- Number of mailbox channels.
- Message width.
- FIFO depth per channel.
- Interrupt target routing policy.
- Whether an overflow raises an error interrupt.
```

**Point to make:** those five open items didn't disappear. Channel count and
FIFO depth became *model constructor parameters* with bounded defaults
(`num_channels=4`, `fifo_depth=8`) recorded in the template's `queues:` section
— so experiments can sweep them and nothing is hard-wired on a guess.

### 5.3 What the reviewed template captured

The DLD's prose "a message is accepted only when the target channel FIFO has
space; a full channel applies backpressure" became a typed command entry in
[templates/mailbox_ip.template.yaml](../templates/mailbox_ip.template.yaml):

```yaml
- name: SEND_MESSAGE
  description: Sender writes a message into a channel FIFO.
  fields: [channel_id, message]
  valid_conditions: [channel_enabled, fifo_has_space]
  completion_conditions: [message_enqueued]
  error_conditions: [fifo_full]
  functional_effects: [enqueue_message, raise_doorbell]
  timing_effects: [push_delay]
```

### 5.4 What the model implements

[src/ip_model_automation/mailbox_ip.py](../src/ip_model_automation/mailbox_ip.py)
— note every line traces back to the template: state names, the bounded
`message_fifo`, the doorbell hand-off, the `REJECT_FULL` drop path.

```python
def message_push_process(self):
    while True:
        self.fsm_state["message_push"] = "IDLE"
        channel_id, message = yield self.sender_if.get()
        self.fsm_state["message_push"] = "ACCEPT_MESSAGE"
        yield self.env.timeout(self.lat["push"])
        self.fsm_state["message_push"] = "CHECK_SPACE"
        fifo = self.message_fifos[channel_id]
        if len(fifo) < self.fifo_depth:
            fifo.append(message)
            self.fsm_state["message_push"] = "ENQUEUE"
            self.metrics["enqueued_messages"] += 1
            yield self.doorbell_queue.put(channel_id)
        else:
            self.fsm_state["message_push"] = "REJECT_FULL"
            self.metrics["dropped_on_full"] += 1
            self.logger.warning("channel fifo full channel=%s message dropped", channel_id)
```

### 5.5 What the test proves

Template scenario:

```yaml
- name: full_fifo_applies_backpressure
  description: Sending beyond FIFO depth drops or stalls messages on a full channel.
  input_sequence: [CONFIG_CHANNEL_0, SEND_MESSAGE_BURST]
  expected_functional_behavior: [dropped_on_full_counted]
  fsm_coverage: [message_push.CHECK_SPACE, message_push.REJECT_FULL]
```

becomes, in [tests/test_mailbox_ip.py](../tests/test_mailbox_ip.py):

```python
def test_full_fifo_applies_backpressure(self):
    env = simpy.Environment()
    model = MailboxIpModel(env, fifo_depth=4)
    model.set_receiver_enabled(False)
    model.configure_channel(0)
    for index in range(6):
        model.send_message(0, f"m{index}")
    env.run(until=20)
    self.assertEqual(model.metrics["enqueued_messages"], 4)
    self.assertEqual(model.metrics["dropped_on_full"], 2)
```

Six sends into a depth-4 FIFO with the receiver stalled: exactly 4 enqueue,
exactly 2 hit `REJECT_FULL`. The DLD's one-line backpressure claim is now
executable.

### 5.6 The traceability table (the slide people remember)

| DLD says | Template captures it as | Model implements it | Test proves it |
| --- | --- | --- | --- |
| "A full channel applies backpressure to the sender" (§4.2) | `SEND_MESSAGE.error_conditions: [fifo_full]`; `queues.message_fifo.blocking_behavior: backpressure` | `message_push_process` `REJECT_FULL` branch | `test_full_fifo_applies_backpressure` |
| "Enqueue precedes doorbell; doorbell precedes interrupt" (§11) | `sequential_paths: message_to_interrupt_path` | `doorbell_queue` → `interrupt_pending_queue` hand-offs | `test_message_enqueue_delivers_and_interrupts` |
| "Doorbell interrupt is level, asserted until software clears" (§4.4) | `interrupt_if.wait_model: wait_for_ack_before_next_request`, `outstanding_limit: 1`, waits in `interrupt_notify.WAIT_SW_CLEAR` | `interrupt_notify` parks in `WAIT_SW_CLEAR`; `clear_interrupt()` releases it | `test_interrupt_waits_for_software_clear_before_next_assertion` |
| Per-FSM cycle counts (§11) | `timing_model.fsm_process_delays` | latency constructor parameters (`self.lat`) | timing assertions |
| Open Items: channel count, FIFO depth (§12) | gaps report + `queues.message_fifo.depth` | `num_channels` / `fifo_depth` parameters | swept by `run_experiments.py` |

---

## 6. The template contract

The template is a YAML with a fixed shape. It captures, regardless of how the
DLD was written:

| Section | What it holds |
| --- | --- |
| `ip` | name, domain, description, clock domains, reset behavior |
| `interfaces` | type, direction, clock domain, **wait model**, transactions |
| `commands` | fields, valid/completion/error conditions, functional + timing effects |
| `fsm_processes` | states, transitions (condition/actions/latency), interfaces touched, queues/resources used |
| `resources` | capacity, bandwidth, latency, arbitration, sharing scope |
| `queues` | producer/consumer, **depth (bounded or explicit `unbounded` + reason)**, blocking behavior, ordering |
| `fsm_relationships` | `fsm_count`, parallel groups, sequential paths, gating relationships |
| `timing_model` | clock, cycle time, per-FSM operation cycles/ns, end-to-end paths |
| `functionality_model` | state variables, APIs, invariants, ignored payload details |
| `performance_model` | what's modeled (backpressure, occupancy, arbitration…) and the metric list |
| `test_scenarios` | input sequence, expected behavior, **`fsm_coverage`** |
| `subsystem` *(optional)* | members and `connections` with ack semantics |

Annotated reference for authors: [templates/reference_template.yaml](../templates/reference_template.yaml).

### Enforced in two non-overlapping layers

Both run by `tools/template_lint.py`:

- **Structure — `schemas/ip_model_template.schema.json`** (real JSON Schema,
  validated with `jsonschema`): which sections/fields exist, their types, value
  constraints. E.g. a queue `depth` is a positive integer or the literal
  `unbounded`, and an unbounded queue must carry a `depth_note`.
- **Cross-field semantics — the linter**: the rules JSON Schema *cannot*
  express. `fsm_count` matches the actual FSM list; every FSM has a timing entry
  and appears in a test scenario; every wait point names a real FSM state; the
  arbitration-IP sub-contract.

Each rule lives in exactly one layer, so schema and linter cannot drift apart.
This is a good detail to mention — it's the answer to "how do you keep the
checker itself honest?"

---

## 7. Interface wait models (the newest piece — worth its own slide)

**The problem:** a template said *which* interfaces an IP has, but not the one
thing that decides where the model blocks — whether the requester waits for a
response, waits in place for an ack, or carries the ack over to the next
request. That difference is the gap between a model that stalls where the real
IP stalls and one that only looks right on average.

Every interface now declares one of three modes:

| Mode | Meaning | Must also state |
| --- | --- | --- |
| `wait_for_response` | Blocks until the response returns, indefinitely, and uses the result | `response_used_for` |
| `wait_for_ack_inline` | Blocks at the request site for an ack, then continues the same pipeline — **the default** | — |
| `wait_for_ack_before_next_request` | Continues after issuing; the ack is collected before the next request | `outstanding_limit` |

Plus: who the requester is (`this_ip` / `peer`), the `<fsm>.<STATE>` wait
points, what releases the wait (`resumes_on`), and `source: dld` vs
`assumed_default`.

### In the DLD (prose the author writes)

```text
Wait model:
- Mode: `wait_for_ack_before_next_request`
- Requester: this IP
- Waits in: `interrupt_notify.WAIT_SW_CLEAR`
- Resumes on: `software_clear`
- Outstanding limit: 1
- Note: the doorbell interrupt does not block the message path; the software
  clear must be seen before the next interrupt is asserted
```

### In the template

```yaml
wait_model:
  mode: wait_for_ack_before_next_request
  requester: this_ip
  wait_points: [interrupt_notify.WAIT_SW_CLEAR]
  resumes_on: software_clear
  outstanding_limit: 1
  source: dld
```

### In the model

```python
self.fsm_state["interrupt_notify"] = "ASSERT_IRQ"
...
clear_event = self.env.event()
self.irq_clear_events[channel_id] = clear_event
self.fsm_state["interrupt_notify"] = "WAIT_SW_CLEAR"
yield clear_event                      # ← the barrier the template declared
self.fsm_state["interrupt_notify"] = "DEASSERT_IRQ"
```

And a `wait_for_response` interface becomes a real round trip the caller yields
on, not a plain Python getter that would hide the access latency:

```python
value = yield model.read_status(channel)
```

### The contract is closed on *both* sides

- `template_lint.py` proves the declared wait point names a real FSM state.
- `check_wait_model_coverage.py` proves the model actually **enters** that state.

Without the second check, a template could claim the requester blocks for a
software clear while the model asserts the interrupt and loops on — and the
numbers would still look plausible. That is exactly the drift that is expensive
to find later. When this gate was introduced it caught six interrupt paths doing
precisely that, plus `completion_ip` publishing no `fsm_state` at all.

**Silence is visible, not silent:** a DLD that states no wait model gets
`wait_for_ack_inline` stamped `source: assumed_default`, it shows up in the gaps
report, and `check_template_coverage --strict` refuses to promote a template
still carrying one.

Live proof:

```bash
python tools/check_wait_model_coverage.py
```

```text
wait model coverage: OK (11 models implement every declared wait point)
```

---

## 8. The gates — what "validated" actually means

```text
template_lint.py                  schema structure + cross-field semantics
  ─► check_template_coverage.py --strict   every DLD FSM + count captured, zero TODO_REVIEW
  ─► report_model_coverage.py             every FSM appears in a test scenario
  ─► check_wait_model_coverage.py         the model enters every declared wait point
  ─► generate_model_scaffold.py           model shape: class + one process per FSM
  ─► unit tests                           functional + timing assertions
  ─► validate_dld_flow.py: OK
```

Plus two repo-wide hard gates in the automated pipeline:

- `check_code_style.py` — ruff lint + format over `src/`, `tools/`, `tests/`
- `run_code_coverage.py --fail-under 95 --fail-under-file 95` — line coverage of
  the models from the unit tests, 95%+ **total and per model file**

Diagram: [diagrams/05_validation_gates.svg](diagrams/05_validation_gates.svg)

One command proves the whole repo is consistent:

```bash
python tools/validate_dld_flow.py
```

Sample of the FSM coverage report, for the "how do I know every FSM is
exercised?" question:

```text
completion_ip: 3/3 FSMs covered
  OK accept: write_debits_write_budget
  OK completion_scheduler: write_debits_write_budget, token_starvation_blocks_completion, output_backpressure_holds_completion
  OK refill: refill_restores_token_window
```

---

## 9. The automated runner

```bash
python tools/auto_ip_pipeline.py                      # every changed DLD
python tools/auto_ip_pipeline.py dlds/my_ip_dld.docx  # one DLD explicitly
python tools/auto_ip_pipeline.py --force              # reprocess everything
```

Things worth calling out:

- **The stage list is data, not code.** `harness/ip_generation_loop.yaml` is the
  single source of truth for the pipeline — adding, removing, or reordering a
  stage is a YAML edit, not a runner change. Show the file; it reads like a
  checklist (`parse_dld`, `review_template`, `check_dld_coverage`,
  `lint_template`, `generate_scaffold`, `generate_prompt_pack`,
  `agent_implementation` / `amend_implementation`, `unit_tests`,
  `stamp_provenance`, then repo-scope `coverage_report`, `wait_model_coverage`,
  `code_style`, `code_coverage`, `full_validation`).
- **Change detection** hashes DLD content into
  `reports/.dld_pipeline_state.json`. An IP is marked *processed* only after its
  full chain passes — a half-finished IP is picked up again next run.
- **`.docx` DLDs are accepted** and converted to markdown first.
- **Greenfield vs brownfield is automatic:** the runner snapshots whether the
  model file existed before any stage ran. New IP → `scaffold` +
  `agent_implementation`. Existing IP whose DLD changed → `amend_implementation`.
- **Only two stages need judgment** — review the template, and implement/amend
  the model. Each declares `gates:` in the YAML: *if the gates already pass, the
  agent is skipped entirely.*
- **Agent-agnostic by design.** Without an agent the runner writes a
  ready-to-send prompt to `reports/agent_requests/<ip>.<stage>.prompt.md` and
  reports the IP as *awaiting*. With one, it pipes the prompt to stdin and
  re-invokes with the failure log while gates fail, up to `max_iterations`:

  ```bash
  python tools/auto_ip_pipeline.py --agent claude
  python tools/auto_ip_pipeline.py --agent codex
  python tools/auto_ip_pipeline.py --agent gemini
  python tools/auto_ip_pipeline.py --agent-cmd "my-agent --auto"
  ```

  Requirements on the agent: runs headless, reads the prompt from stdin, can
  edit files, exits when done. **Whichever agent (or human) writes the code, the
  same gates judge it.**

Diagram: [diagrams/06_auto_pipeline.svg](diagrams/06_auto_pipeline.svg)

---

## 10. When the DLD changes — amend, don't regenerate

The insight: **don't diff the DLD** (prose — reworded, reordered, noisy). Diff
the **template**. Because it's normalized, two revisions produce a clean, typed
change list that maps almost one-to-one to model and test edit sites.

```bash
git show HEAD:templates/mailbox_ip.template.yaml > old.yaml
python tools/diff_template.py old.yaml templates/mailbox_ip.template.yaml
python tools/diff_template.py old.yaml templates/mailbox_ip.template.yaml --amend-prompt --ip mailbox_ip
```

Real shape of the output:

```text
4 change(s), overall blast radius: STRUCTURAL

  [STRUCTURAL] FSM `doorbell` states: +['COALESCE'] -[]
  [SURGICAL  ] queue `message_fifo`.depth: 8 -> 16
  [SURGICAL  ] timing op `message_push.enqueue`: 1cyc/2ns -> 2cyc/4ns
  [SURGICAL  ] test_scenario `fifo_high_watermark_backpressure` added
```

- **SURGICAL** = a localized value or addition (queue depth, a timing number, a
  new scenario).
- **STRUCTURAL** = a topology change (FSM, state, interface, command, or a
  *changed wait-model mode* — the stall sites move). One structural change
  escalates the whole delta, so the implementer reviews before editing.

`--amend-prompt` wraps this into: *"here is the current model and tests, here is
exactly what changed — make the minimal corresponding edits, do not rewrite."*

Two supporting pieces:

- **Git is the history store.** The "previous" template is just the last
  committed one. No separate version store.
- **`check_model_provenance.py`** records, per IP, the hash of the template each
  model was last built or amended against (`templates/model_baselines.json`,
  committed). Run with no args it reports any template that has drifted from its
  model's baseline — that's the signal an amend is due. `--stamp <ip>` refreshes
  it after the amend. Deliberately one IP at a time: the stamp *asserts* the
  model was amended against that revision, a claim the hash comparison itself
  cannot verify. (`--stamp-all` is bootstrap-only and refuses to run once
  baselines exist.)

---

## 11. Subsystems: "just another IP"

A subsystem (several IPs wired together) rides the *same* DLD → template →
model → tests pipeline, so every gate applies unchanged. Its FSM processes are
the glue; the member IP models are its resources.

Two exist today:

- **`dma_subsystem`** — composes `gdma_ip`, `axi_interconnect_ip`,
  `arbitration_ip`, `completion_ip`. Descriptors fan out to an engine leg and a
  fabric leg; the subsystem IRQ fires when both legs complete. A backpressure
  monitor couples fabric congestion to GDMA memory readiness and completion
  backlog to arbitration issue readiness.
- **`mailbox_irq_subsystem`** — `mailbox_ip` (doorbell source) +
  `interrupt_controller_ip` (delivery fabric) + a modeled software service loop
  (ack, read, clear, EOI), with true level-triggered semantics and duty-cycle
  storm throttling.

Extra check: `python tools/check_subsystem_wiring.py` — members exist, the
member APIs and glue FSMs named in `connections:` are real, and the model
actually instantiates its members.

Connections declare **ack semantics** (`none` / `completion_event` /
`level_until_serviced`, with `ack_via` naming the return path):

```yaml
- {from: mailbox_ip.interrupts, to: irq_source_bridge, payload: doorbell_irq,
   ack: completion_event, ack_via: irq_source_bridge -> mailbox_ip.clear_interrupt}
- {from: irq_source_bridge, to: interrupt_controller_ip.set_level, payload: level_assert,
   ack: level_until_serviced, ack_via: software_handler -> ... + interrupt_controller_ip.eoi}
```

**A great war story for the talk:** when the wait-model barriers went in, the
mailbox's one-outstanding-doorbell rule serialized this subsystem and silently
disabled its level-reassert and storm-throttle behavior. The fix was a design
decision recorded in the subsystem's own DLD — the bridge acknowledges the
doorbell as it hands the level to the controller. The same work exposed the
interrupt controller queuing stale delivery winners, invisible until something
actually waited on them. *That's the point of modeling real stall semantics.*

---

## 12. Portability — pointing the tooling at another codebase

Where models, tests, templates, and DLDs live — and how a model file and class
are named — is declared in `target_profile.yaml`, not hardcoded:

```yaml
paths:   {model_dir: sim/models, tests_dir: sim/tests, templates_dir: specs}
naming:  {model_file: "{ip}_model.py", model_class: "Sim{camel}"}
```

With that profile, `profile.model_file("mailbox_ip")` →
`sim/models/mailbox_ip_model.py`, `profile.model_class("mailbox_ip")` →
`SimMailboxIp`. The same extraction, generation, diff/amend, and validation
tools then run against a different SimPy framework with **no tool-code change**.

State it accurately: this is an **in-progress seam**. Per-IP artifact paths and
the model-class convention already route through the profile; remaining
hardcoded paths are being migrated incrementally, and each still resolves to the
same default, so nothing breaks meanwhile.

---

## 13. What else you get for free

- **Readable specs** — `python tools/render_template_doc.py --format both`
  renders every template as Markdown + standalone HTML in
  `reports/template_docs/`. Good for review meetings; no YAML reading required.
- **Performance experiments** — `python tools/run_experiments.py` sweeps one
  model parameter against a fixed deterministic workload (descriptor latency vs.
  interconnect outstanding limit, completion delay vs. QoS token budget, message
  round-trip vs. storm throttle window, issued commands vs. burst credit). One
  CSV per experiment plus a combined `summary.md`.
- **IP-tagged logging** — every model takes `log_level` / `log_file`:

  ```text
  2026-07-03 10:00:00,000 INFO [arbitration_ip] issued port=port0 tenant=T0 sq=SQ0 issue_count=1 cmds=['cmd0']
  ```

- **A `metrics` dict per model** that the unit tests assert on — counts,
  latencies, occupancies.
- **Coding style is enforced, not suggested** — one written guide
  (`skills/ip-model-generation/references/coding_style.md`), machine half in
  `ruff.toml`, run as a hard pipeline gate. Prompt packs carry the style rules,
  so newly generated models follow it from the first line.

---

## 14. Suggested 15-minute live demo

```bash
# 1. The whole repo is consistent — one command, one answer  (~1 min)
python tools/validate_dld_flow.py

# 2. What an IP's spec looks like normalized
python tools/render_template_doc.py templates/mailbox_ip.template.yaml --stdout

# 3. The contract is real: break something and watch the gate fail
#    (e.g. change fsm_count to 4 in a copy of the template)
python tools/template_lint.py templates/mailbox_ip.template.yaml

# 4. The two-sided wait-model contract
python tools/check_wait_model_coverage.py

# 5. Every FSM is exercised by a scenario
python tools/report_model_coverage.py

# 6. A DLD edit produces a typed, blast-radius-tagged delta
git show HEAD:templates/mailbox_ip.template.yaml > old.yaml
python tools/diff_template.py old.yaml templates/mailbox_ip.template.yaml

# 7. The tests that came out of the scenarios
python -m unittest tests.test_mailbox_ip -v
```

For maximum effect, do #3 for real: edit a promoted template so `fsm_count`
disagrees with the FSM list, run the lint, show the failure, revert. The team
believes the gates once they've seen one bite.

---

## 15. Questions you should expect

**"Is an LLM writing our models? How can we trust that?"**
The LLM fills two judgment steps — resolving `TODO_REVIEW` markers and writing
model/test code from the prompt pack. It never sees a shortcut around the gates:
template coverage, lint, wait-model coverage, unit tests, ruff, 95% code
coverage. A human doing those two steps faces exactly the same gates. And the
pipeline is vendor-neutral — Claude, Codex, Gemini, or an in-house agent, chosen
by a flag.

**"What if the DLD is wrong or ambiguous?"**
Then it shows up as a gap, not as a model. The gaps report lists what the
extractor couldn't derive plus the DLD's own Open Items, and `--strict` coverage
refuses to promote a template with unresolved markers or an assumed-default wait
model. The system's failure mode is *"tell me more,"* not *"guess."*

**"Do the timing numbers actually match the model?"**
Structure is provably faithful. Timing numbers are a reviewed reference checked
for internal coherence (`ns == cycles × cycle_time_ns`, declared queue capacities
are real) — but constructor defaults in the models are deliberately low so tests
run in a few simulated ticks, with template values passed as overrides. Say this
plainly; it's a known, documented boundary, not an oversight.

**"What happens when the spec changes six months from now?"**
`check_model_provenance.py` tells you which models drifted from their template
baseline. `diff_template.py` tells you exactly what changed and how far the blast
radius reaches. The pipeline takes the amend path automatically and edits in
place instead of regenerating.

**"How much work is a new IP?"**
Write the DLD in the shape the extractor understands (copy any file in `dlds/`),
drop it in, run the pipeline. `mailbox_ip`, `spi_master_ip`, and `i3c_ip` were
added exactly this way — brand-new DLDs in, validated models and tests out.

**"Can we use this on our other simulation codebase?"**
That's the `target_profile.yaml` seam — partially landed, see §12.

**"Why SimPy?"**
Transaction-level delay modeling with concurrent processes is exactly SimPy's
model: one FSM = one process, queues and stores between them, `env.timeout()`
for cycle costs. It makes the template's FSM/queue structure map to code almost
literally, which is what makes generation and checking tractable.

---

## 16. Glossary

| Term | Meaning |
| --- | --- |
| **DLD** | Design-Level Document — the human-written spec for an IP block; the *authoring* source |
| **Template** | Normalized YAML spec; the *golden reference* for generation |
| **Draft vs promoted** | `*.template.draft.yaml` = extractor output awaiting review; `*.template.yaml` = reviewed, gated, golden |
| **Gaps report** | Per-IP list of what the DLD didn't state (`reports/<ip>.gaps.md`) |
| **`TODO_REVIEW`** | Marker for a field the extractor refused to guess; blocks promotion |
| **Wait model** | How the requester waits on an interface — response / inline ack / deferred ack |
| **Wait point** | `<fsm>.<STATE>` where that wait actually happens; proven on both sides |
| **Blast radius** | `SURGICAL` (localized) vs `STRUCTURAL` (topology) tag on a template diff |
| **Provenance / baseline** | Hash of the template each model was last built against |
| **Greenfield / brownfield** | New IP (generate) vs existing IP with a changed DLD (amend) |
| **Prompt pack** | Self-contained bundle (template + contract + skill) handed to the implementer |
| **Gate** | An automated check that must pass before the next stage runs |

---

## 17. One-slide summary

- You write a **DLD**; the system produces a **validated SimPy model** and
  **unit tests**.
- A **normalized template** sits in the middle as the golden reference — the DLD
  authors it, the template generates everything else, and the DLD is never read
  again after promotion.
- **Deterministic tools** do extraction, linting, coverage, and validation; a
  **human or any vendor's LLM agent** fills exactly two judgment steps, under
  the same gates.
- **Missing details are surfaced**, never invented.
- **Interfaces declare where they stall**, and the gate proves the model
  actually stalls there.
- **Every stage has a gate**, and one command — `validate_dld_flow.py` — proves
  the whole repo is consistent.
