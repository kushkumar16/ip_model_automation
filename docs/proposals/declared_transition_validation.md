# Proposal: check declared transitions at runtime, and gate what is left over

**Status:** step 1 done, steps 2–3 **deferred by decision on 2026-08-09** — see
§7. `tools/check_declared_transitions.py` exists and reports; it is not a gate,
and no gate is planned for now. `--emit-findings` can file an IP's disagreements
as blocking findings when someone chooses to work that IP, but nothing files them
automatically and no findings file is committed for any IP.

**Problem it addresses:** six of the eleven model-review findings on
`sram_ctrl_ip` are the same mechanical question — *did the model take a
transition the template declares?* — and every one of them was found by an LLM
reading code, one cycle at a time, after every deterministic gate was green.

| Finding | The deviation | Side that was wrong |
| --- | --- | --- |
| M2 | `READ_ENTRY -> SCRUB_WAIT` taken, never declared | template |
| M3 | `QUEUE_FULL -> PUSH_QUEUE` taken, `-> ACCEPT_IDLE` declared | model |
| M7 | `WAIT_ECC` exit on uncorrectable RMW, never declared | model |
| M8 | `DECODE_BANK -> ACCEPT_IDLE` taken, never declared | template |
| M9 | `SCHED_IDLE` re-entered; declared edges never taken | model |
| M11 | `SCHED_IDLE` re-entered again, via a stale event | model |

The tests already drive every one of these paths. Nothing watches.
`report_model_coverage.py` reads the template's own `fsm_coverage` declarations
and never runs the model; `check_template_coverage.py` compares the DLD to the
template, not the template to the model. So the transition list — the most
precisely specified part of the contract — is the part with no automated check
against the thing it specifies.

---

## 1. The check

`_set_fsm_state(fsm, state)` already exists in every generated model and is the
single point every transition passes through. It knows the state it is leaving
and the state it is entering. The template's `fsm_processes[].transitions` is a
list of exactly those pairs.

    validator = DeclaredTransitions.from_template("templates/<ip>.template.yaml")
    ...
    def _set_fsm_state(self, fsm, state):
        self.deviations += validator.check(fsm, self.fsm_state[fsm], state)
        ...

Undeclared `(from, to)` pairs are recorded, not raised. A declared pair never
taken across a whole test run is the same finding from the other side (M9's
`ISSUE_ACCESS -> PICK_BANK`), and falls out of the same data for free.

## 2. Graceful at runtime, hard in CI

A model that raises mid-simulation is worse than one that deviates: the
deviation is a documentation defect, and aborting the run destroys the results
that would show what the deviation *did*. So:

* **Runtime** — record to `metrics["undeclared_transitions"]`, log at WARNING
  with both states and the FSM. The simulation completes and stays usable.
* **CI** — a new gate fails on any recorded deviation that is not ratified. The
  run is honest; the build is not lenient.

This is the split the repo already uses for `TODO_REVIEW`: the marker is
harmless in a draft, and `check_template_coverage.py --strict` refuses to promote
one that still carries it.

## 3. Ratified deviations, and why they need an id

Not every deviation is a defect. M7's resolution was that an uncorrectable RMW
still issues its write half — a decision the DLD does not make, ratified by a
person, recorded in `decisions/`. A deviation that has been through that is not
the same as one nobody has seen.

So the allowlist is keyed to the decision, not to the code:

    # deviation: M7 -- ratified in decisions/sram_ctrl_ip.md
    self._set_fsm_state("bank_scheduler", "RMW_MERGE")

The gate parses these, requires each id to resolve to a heading in
`decisions/<ip>.md`, and fails on a marker that names nothing — the same
discipline `check_review_findings.py` applies to a dismissal. A bare `# TODO`
would not survive contact with this repo: it is invisible to every gate, so it
rots quietly, which is the failure mode the findings gate was built to end.

## 4. What this does not do

It does not replace the review stage. Five of the eleven findings — M1
(`capacity_mismatch`), M4 and M10 (`test_asserts_wrong_thing`), M5 and M6
(`scenario_gap`) — are not transition questions and no runtime check would see
them. M10 in particular is a test that cannot fail, which is exactly what a
deterministic checker is blind to.

What it does is stop the reviewer spending four cycles on the class a machine can
decide, so the cycles it does cost are spent on the classes it is uniquely good
at.

---

## 5. The post-review script, and the part of it to refuse

A script should drive the loop around a review. It must not decide the findings.

**Mechanical, worth automating** — for each finding, once a person has recorded
a resolution: drive the amend agent, re-stamp provenance, re-run the gates,
re-run the reviewer, report. That is four manual steps per finding per cycle, and
this branch has done them eleven times.

**Not automatable** — *which side is wrong*. On `sram_ctrl_ip` the answer split
four ways: template (M1, M2, M8), model (M3, M9, M11), tests (M4, M10), scenario
inputs (M5, M6). It cannot be read off the finding, because the finding reports a
disagreement and the resolution is a judgement about intent.

M7 is the case that settles it. The finding was that the model abandoned an RMW's
write half on an uncorrectable read. The first resolution corrected the
*template* to match the model, with a written argument about not writing into a
line just declared unusable. The author reversed it: allow the write, the
controller does not decide that on the requester's behalf. The model was wrong
all along.

An auto-fixer would have taken the first answer — plausible, argued, wrong — and
recorded it as closed. Worse, it would be the same agent writing the fix and
certifying it, which is the loop `agents/model_review_agent.md` exists to break
and the reason its rule 6 is *do not fix anything*.

So the script's shape is: **a person writes a one-line resolution per finding;
the script does everything after that.**

---

## 6. What the first run found

`python tools/check_declared_transitions.py`, against `develop`, using each IP's
own test module as the stimulus:

```text
total: 227 undeclared, 29 declared-but-never-taken
```

Split by shape:

| | count |
| --- | --- |
| both states declared, the edge between them is not | 199 |
| a state re-entered from itself | 28 |
| a state the template never lists | 0 |

The last row is the reassuring one: no model is in a state its contract has never
heard of. Every deviation is an edge, not a state — which is why the transition
list is the right thing to check and why the state list alone was never going to
find these.

**The dominant shape is the return edge.** `ENQUEUE_READ_REQ -> WAIT_AR`,
`GRANT_READ -> SCAN_MASTERS`, `ISSUE_ACCESS -> SCHED_IDLE` — a process finishes a
unit of work and loops back to its wait state, and templates routinely declare
the forward path and omit the way back. So 227 is not 227 independent defects; it
is closer to one systematic gap in how transition lists are written, instanced
227 times. That is an argument for fixing the extraction and the template
contract, not for filing 227 findings.

**`sram_ctrl_ip` has the fewest.** Six, against 33 for `axi_interconnect_ip` and
31 each for `gdma_ip` and `timer_ip` — and it is the only IP that has been
through four LLM review cycles. The reviews were doing real work; they were just
doing it one IP and one cycle at a time.

**Against the known findings**, on a `develop` checkout that predates the fixes,
the tool reproduces M2 (`READ_ENTRY -> SCRUB_WAIT`), M3 (`QUEUE_FULL ->
PUSH_QUEUE`, with the declared `-> ACCEPT_IDLE` reported as never taken), M8
(`DECODE_BANK -> ACCEPT_IDLE`), M9 (`ISSUE_ACCESS`/`RETURN_DATA -> SCHED_IDLE`,
with both declared `-> PICK_BANK` edges never taken) and M11 (`SCHED_IDLE ->
SCHED_IDLE`) — five of the six transition findings, in under a second, with no
model changed and no LLM involved.

It does **not** find M7. Nothing on `develop` forces an uncorrectable ECC error on
an RMW, so the deviating path is never executed. That is the method's real limit:
it sees only what the tests drive, which makes it a complement to the reviewer
rather than a replacement, and makes the `never taken` column worth as much
attention as the `undeclared` one.

## 7. Suggested order

1. ~~The checker, reporting only.~~ Done — `tools/check_declared_transitions.py`.
   It instruments each model's `fsm_state` mapping rather than a setter, because
   two models route changes through a `_set_fsm_state` helper and ten assign the
   dict directly.
2. Decide what a gate refuses. Not "any undeclared transition" — that fails
   twelve IPs on day one. The candidates are: fail on same-state re-entry (28,
   nearly all artifacts of a loop's shape), fail per-IP once an IP is clean
   (`sram_ctrl_ip` first), or fix the return-edge gap at the source and then fail
   on the remainder.
3. The `# deviation: <id>` allowlist, tied to `decisions/<ip>.md`.
4. The post-review driver (§5).

Step 2 is where this stops being free, and it needs the same call the findings
gate needed: what does a red build mean, and who is allowed to make it green.

---

## 8. Decision, 2026-08-09: neither pile is being resolved

Both categories the checker separates are **deferred, deliberately and on the
record**. Nothing here is a defect list awaiting triage; it is a measurement of
how far the transition graphs are from describing the models, taken once and left
in place.

| | count | status |
| --- | --- | --- |
| Disagreement — both sides describe behaviour, differently | 54 | not adjudicated |
| Gap — the template gives the state no exit at all | 173 | not modelled, not filled in |
| Declared but never driven by any test | 29 | not investigated |

**Why the two are not the same question.** A gap cannot be modelled without
inventing behaviour, which the contract forbids outright — so deferring it is the
only correct action, and would be even if there were time. A disagreement is
different: the model already does something, so deferring means the model's
behaviour stands on its own authority rather than on the template's. That is a
real cost, accepted knowingly.

**What this means for anyone reading a model's numbers.** For the states listed
by `python tools/check_declared_transitions.py`, the template's transition list
does not describe what the model does. Latency, `bank_utilization`,
`transition_counts` and queue figures are still produced for those paths, and
they are produced by the model's own logic, not by anything the contract states.
M7 is the worked example of why that matters: the model completed an RMW without
its write half, reported 11 cycles instead of 17 and one bank touch instead of
two, and passed 159 tests, coverage, lint and provenance. Nothing said the figure
came from a path nobody had agreed on.

**No findings were filed.** Filing 83 blocking findings in order to dismiss 83 of
them writes no information and spends the one thing dismissal is for — a sentence
of real thought per item. `--emit-findings` remains available per IP, for
whichever IP someone decides to actually work; that is when the ids earn their
keep.

**What would reopen this.** The DLD authors answering what a state's exits are,
for any IP; or a decision to gate one clean IP and grow from there — for which
`sram_ctrl_ip` is still the only candidate, at 6 disagreements and 0 gaps.
