# Model Review Agent Contract

Use this contract for any human, LLM, or scripted agent running the
`review_model` stage. The task is to read a generated SimPy model and its tests
against the template they were generated from, and report where they disagree.

## The rule that governs this stage

> **You may fail a model. You may never pass one.**

Findings are evidence; silence is not. An empty findings list records that a
reviewer looked and reported nothing — a weaker claim than "this model implements
its template", and it must never be read as the stronger one. Nothing you write
can approve a model or substitute for the gates.

That asymmetry is what makes it safe to put a sampled judgement next to a
deterministic pipeline: a wrong finding costs a person a few minutes, a missed
one leaves the system exactly where it was, and no output of yours can wave
anything through.

## Why this stage exists

Every other gate on the model shares one blind spot: **the tests were written by
the same agent that wrote the model.** Unit tests pass, every FSM has a scenario,
the model enters every declared wait point, coverage is above threshold, style is
clean — and a model that misimplements its contract alongside a test that agrees
with it satisfies all of them.

The first run of this review on `sram_ctrl_ip` found six real disagreements after
all of that was green, including a declared capacity never enforced, three
undeclared FSM transitions, and an assertion that could not fail for any
implementation. See `decisions/sram_ctrl_ip.md`.

## Inputs

- Template (**the contract**): `templates/<ip>.template.yaml`
- Model: `src/ip_model_automation/<ip>.py`
- Tests: `tests/test_<ip>.py`

The DLD is deliberately **not** an input. Once promoted, the template is the
source of truth for generation; a disagreement between template and DLD is a
different review's job (`review_normalization`).

## What to report

| Class | The failure |
| --- | --- |
| `timing_mismatch` | A delay in the model differs from the template's stated `cycles`/`ns` for that operation |
| `capacity_mismatch` | A queue depth, outstanding limit, or resource capacity differs from the template |
| `fsm_structure` | A transition or condition the model takes that the template does not declare, or a declared one the model never takes |
| `wait_model_impl` | An interface's blocking behaviour implemented differently from its declared mode |
| `test_asserts_wrong_thing` | A test that passes while asserting something the template does not say, or that cannot fail |
| `scenario_gap` | A template `test_scenario` whose stated behaviour the test named for it does not actually exercise |

The vocabulary is closed and `tools/check_review_findings.py` enforces it. An
unknown class fails the file rather than being ignored — there is no "other"
bucket for vague observations to collect in.

## Rules

1. **Report nothing if you find nothing.** An empty list is the expected result
   for a faithful model. Do not pad; every weak finding costs the same human
   attention as a real one and spends the credibility of the next.
2. Cite both sides. A finding without a template location and a model or test
   location is not checkable, and will be rejected.
3. Do not report style, naming, structure, performance, or design opinions.
4. Detail the template leaves open is not a finding. The model is allowed to
   decide what the contract does not.
5. Severity: `high` if simulated behaviour or timing would be wrong, `medium` if
   a reader or a test would be misled, `low` otherwise.
6. **Do not fix anything.** This stage reads and reports. Fixing here would mean
   the same agent writing and approving a change, which is the loop this exists
   to break.

## Output

Write `reviews/<ip>.model.findings.yaml`:

```yaml
ip: <ip>
kind: model
template_sha256: <sha256 of templates/<ip>.template.yaml>
model_sha256: <sha256 of src/ip_model_automation/<ip>.py>
tests_sha256: <sha256 of tests/test_<ip>.py>
findings:
  - id: M1
    class: timing_mismatch
    severity: high
    claim: <one sentence>
    template: <field and value>
    model: <file:line and value>
    why: <what goes wrong because of it>
```

The hashes make the review a statement about specific files. Edit any of them and
the review is stale, and the gate says so rather than letting an old clean review
vouch for something that has changed. Finding ids must be unique across all of an
IP's reviews, because a dismissal names an id and one line must not clear two
different findings.

## What happens next

`tools/check_review_findings.py` requires every finding to be **fixed** — which
changes a file, marks the review stale, and requires a fresh one — or **dismissed
by name** in `decisions/<ip>.md` with a reason. Dismissal costs a sentence on the
record, deliberately: a finding that can be waved away silently is one nobody has
to think about.

Neither is yours to do. You report; a person decides.
