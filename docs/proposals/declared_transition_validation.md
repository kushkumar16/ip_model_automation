# Proposal: check declared transitions at runtime, and gate what is left over

**Status:** step 1 done, steps 2–3 **deferred by decision on 2026-08-09** — see
§7. `tools/check_declared_transitions.py` exists and reports; it is not a gate,
and no gate is planned for now. `--emit-findings` can file an IP's disagreements
as blocking findings when someone chooses to work that IP, but nothing files them
automatically and no findings file is committed for any IP.

**Problem it addresses:** a recurring class of model-review finding is the same
mechanical question — *did the model take a transition the template declares?*
— and every one of them has been found by an LLM reading code, one cycle at a
time, after every deterministic gate was green. Round five's `arbitration_ip`
review (`M31`–`M33`) is a concrete case: fixing `CREDIT_REFILL`'s retry path
required tracing exactly which `(from, to)` pairs a multi-candidate scan took,
because the template declared the retry edge (`CREDIT_REFILL -> TENANT_SCAN`)
but an early draft of the fix took `TENANT_SCAN -> TENANT_SCAN` instead — an
edge no template anywhere declares, and the kind of thing a person has to read
code to notice.

The tests already drive every one of these paths. Nothing watches.
`report_model_coverage.py` reads the template's own `fsm_coverage` declarations
and never runs the model; `check_template_coverage.py` compares the DLD to the
template, not the template to the model. So the transition list — the most
precisely specified part of the contract — is the part with no automated check
against the thing it specifies.

---

## 1. The check

`_set_fsm_state(fsm, state)` (or a direct `fsm_state[...] = ...` assignment,
where a model does not route through a helper) is the point every transition
passes through. It knows the state it is leaving and the state it is entering.
The template's `fsm_processes[].transitions` is a list of exactly those pairs.

    validator = DeclaredTransitions.from_template("templates/<ip>.template.yaml")
    ...
    def _set_fsm_state(self, fsm, state):
        self.deviations += validator.check(fsm, self.fsm_state[fsm], state)
        ...

Undeclared `(from, to)` pairs are recorded, not raised. A declared pair never
taken across a whole test run is the same finding from the other side, and
falls out of the same data for free.

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

Not every deviation is a defect — a review finding can be legitimately dismissed
rather than fixed, exactly the way `decisions/<ip>.md` already records for
`check_review_findings.py`. A deviation that has been through that judgement is
not the same as one nobody has seen.

So the allowlist is keyed to the decision, not to the code:

    # deviation: M30 -- ratified in decisions/arbitration_ip.md
    self._set_fsm_state("arbiter_main", "CREDIT_REFILL")

The gate parses these, requires each id to resolve to a heading in
`decisions/<ip>.md`, and fails on a marker that names nothing — the same
discipline `check_review_findings.py` applies to a dismissal. A bare `# TODO`
would not survive contact with this repo: it is invisible to every gate, so it
rots quietly, which is the failure mode the findings gate was built to end.

## 4. What this does not do

It does not replace the review stage. Findings of class `capacity_mismatch`,
`test_asserts_wrong_thing`, `wait_model_impl`, and `scenario_gap` are not
transition questions and no runtime check would see them — round five's
`completion_ip` M35 (a RESET-latency test whose middle assertion could not
discriminate a 1-cycle RESET from a 0-cycle one) is exactly the kind of thing a
deterministic transition checker is blind to: both the passing and failing
implementations take the identical `RESET -> READY` edge.

What it does is stop the reviewer spending cycles on the class a machine can
decide, so the cycles it does cost are spent on the classes it is uniquely good
at.

---

## 5. The post-review script, and the part of it to refuse

A script should drive the loop around a review. It must not decide the findings.

**Mechanical, worth automating** — for each finding, once a person has recorded
a resolution: drive the amend agent, re-stamp provenance, re-run the gates,
re-run the reviewer, report. That is four manual steps per finding per cycle,
and this repo has done them dozens of times across five review rounds on two
IPs.

**Not automatable** — *which side is wrong*. Round five's `arbitration_ip` M30
is the case that shows why: the finding was that a declared `latency_cycles: 1`
on `CREDIT_REFILL -> TENANT_SCAN` didn't match the 10-cycle `credit_refill`
operation cost the model actually charges. The obvious-looking fix is to change
one of the two numbers to match the other. The actual resolution was that
`latency_cycles` and `timing_model.fsm_process_delays` are two different,
intentionally separate mechanisms in this project's templates — nearly every
edge in the template declares `latency_cycles: 1` as boilerplate, and the real,
named per-operation cost lives in `fsm_process_delays` instead. Comparing them
was comparing the wrong field to the right one; there was no timing defect to
fix at all. An auto-fixer would have "corrected" a number that was never wrong.

So the script's shape is: **a person writes a one-line resolution per finding;
the script does everything after that.**

---

## 6. What a run looks like

`python tools/check_declared_transitions.py`, against this repo's two live
IPs, using each one's own test module as the stimulus:

```text
arbitration_ip: 23/23 declared transitions taken, 0 never taken
completion_ip: 18/18 declared transitions taken, 0 never taken
```

Both are clean today, but the undeclared column is never zero — each model
also takes a handful of edges the template does not name, almost all of them
same-state re-entries (`arbiter_main: IDLE -> IDLE`, `issue_pipeline:
ISSUE_STALL -> ISSUE_STALL`) or exits from a state the template declares no
exit for at all (`issue_pipeline: RESET`, `accept: ENQUEUE`) — a template gap,
not a model defect, and the tool says so by marking those `NO DECLARED EXIT`
rather than filing a finding: the state's own exit isn't specified, so nothing
was contradicted.

**The dominant shape, when a model does drift, is the return edge.** A process
finishes a unit of work and loops back to its wait state, and a template written
by extraction routinely declares the forward path and omits the way back. That
is an argument for fixing the extraction and the template contract at the
source, not for filing one finding per instance.

## 7. Suggested order

1. ~~The checker, reporting only.~~ Done — `tools/check_declared_transitions.py`.
   It instruments each model's `fsm_state` mapping rather than a setter, since
   not every model routes state changes through a `_set_fsm_state` helper.
2. Decide what a gate refuses. Not "any undeclared transition" — every IP still
   carries a few same-state re-entries and no-declared-exit gaps that are not
   defects. The candidates are: fail on same-state re-entry once a template
   declares an exit for that state, fail per-IP once an IP is clean, or fix the
   return-edge gap at the source and then fail on the remainder.
3. The `# deviation: <id>` allowlist, tied to `decisions/<ip>.md`.
4. The post-review driver (§5).

Step 2 is where this stops being free, and it needs the same call the findings
gate needed: what does a red build mean, and who is allowed to make it green.

---

## 8. Decision, 2026-08-09: neither pile is being resolved

Both categories the checker separates are **deferred, deliberately and on the
record**. Nothing here is a defect list awaiting triage; it is a measurement of
how far the transition graphs are from describing the models, taken and updated
as the repo's IPs change, not resolved wholesale.

**Why the two are not the same question.** A gap (a state the template gives no
exit at all) cannot be modelled without inventing behaviour, which the contract
forbids outright — so deferring it is the only correct action, and would be even
if there were time. A disagreement (both sides describe behaviour, differently)
is different: the model already does something, so deferring means the model's
behaviour stands on its own authority rather than the template's. That is a real
cost, accepted knowingly, and it is why round five's M30 (above) was worth
tracing all the way through rather than dismissing on sight.

**What this means for anyone reading a model's numbers.** For a state the tool
lists as `NO DECLARED EXIT`, the template's transition list does not describe
what the model does there. Latency, `transition_counts`, and queue figures are
still produced for those paths, and they are produced by the model's own logic,
not by anything the contract states.

**No findings are filed automatically.** Filing a blocking finding for every
same-state re-entry or no-declared-exit gap in order to dismiss most of them
writes no information and spends the one thing dismissal is for — a sentence of
real thought per item. `--emit-findings` remains available per IP, for whichever
IP someone decides to actually work; that is when the ids earn their keep.

**What would reopen this.** The DLD authors answering what a state's exits are,
for either live IP; or a decision to gate transitions once an IP's undeclared
count reaches zero and stays there across a review round.
