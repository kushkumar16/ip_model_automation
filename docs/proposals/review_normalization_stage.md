# Proposal: a `review_normalization` stage between the gate and the stamp

**Status:** built. Steps 1–2 were the gate, the findings format and the mutation
test (§8). The stage itself now exists: `agents/normalization_review_agent.md`
holds the contract, and `review_normalization` is wired into the harness between
`check_normalization` and `check_normalization_stamp`, gated by
`check_review_findings` so a finding blocks the stamp rather than entering
`normalize_dld`'s retry loop.

The findings file is `reviews/<ip>.normalization.findings.yaml` rather than the
`reviews/<ip>.findings.yaml` named in §5 below — the gate became multi-kind when
`review_model` was added, and each kind now names itself in its filename and in a
`kind:` field. Everything else below is as built.

What remains is not construction: no agent has been run against a real IP through
this stage yet, and the one real finding from the §8 experiment is recorded and
resolved. The larger mutation set that would establish the reviewer's *rate*,
rather than that a signal exists, is still owed.

**Problem it addresses:** one class of error survives everything the pipeline
currently checks. `check_dld_normalization.py` proves nothing was dropped,
altered, or invented — every measurement conserved, every identifier conserved,
every source block accounted for. It cannot prove the surviving claims are
*attached to the right things*. A normalization can preserve `5 cycles` perfectly
and hang it on the wrong FSM, and every gate in the repo stays green while the
generated model runs with the wrong delay.

Per-FSM state parity closed this for **states** (see
[normalize_dld_stage.md](normalize_dld_stage.md) §4). Nothing closes it for
timings, wait models, or command effects. That is the residual risk the
normalization stamp exists to absorb, and it is currently absorbed by a human
reading two documents side by side with no help.

---

## 1. Where it sits

```text
normalize_dld               (agent — reshapes the document)
  -> check_normalization    (deterministic — conservation, parity, provenance)
  -> review_normalization   ★ NEW — agent stage, findings only
  -> check_review_findings  ★ NEW — deterministic — every finding accounted for
  -> check_normalization_stamp  (human — the meaning survived)
  -> parse_dld ...
```

It runs *after* the mechanical gate, because there is no point asking an LLM
whether the meaning survived a normalization that has already lost a number. It
runs *before* the stamp, because its whole purpose is to make that human review
better informed.

---

## 2. The asymmetry: it may fail, never pass

> **A reviewer may fail a normalization. It may never pass one.**

Findings are evidence. Silence is not. A reviewer that reports nothing has told
us nothing — not that the normalization is faithful — and the report must say so
in those words, because "no findings" will otherwise be read as a green light by
the next person in a hurry.

This is the property that makes it safe to put an LLM next to a gate at all.
Under it:

- a **wrong** finding costs human attention and nothing else,
- a **missed** finding leaves the system exactly where it is today,
- and a reviewer can never substitute for the human stamp, because it has no way
  to express approval.

It is also why the reviewer does not need a reviewer. The usual objection to
LLM-judges-LLM — correlated blind spots, the same model missing the same things
twice — is an argument against trusting a *pass*. It has no force against a
*finding*, which is a specific, checkable claim a human can confirm or dismiss in
seconds.

---

## 3. The crux: the LLM writes an artifact, a deterministic gate reads it

Every gate in this repo today is deterministic. That is not decoration: it is
why a gate failure is actionable, why `max_attempts` terminates, and why a green
result means the same thing on Tuesday as it did on Monday. Putting a sampled
verdict inside CI would give that up.

So the reviewer never votes:

| Component | Nature | Produces |
| --- | --- | --- |
| `review_normalization` | agent, non-deterministic | `reviews/<ip>.findings.yaml` |
| `check_review_findings` | tool, deterministic | pass/fail, read from that file |

The gate asks a question with a mechanical answer: **is every recorded finding
accounted for?** A finding is accounted for when it has been fixed (the
normalization changed, which invalidates the review and forces a re-run) or
explicitly dismissed by a human in `decisions/<ip>.md`, naming the finding and
giving a reason.

Re-running CI re-reads a file. The LLM runs when someone runs it. The gate's
answer is the same every time, and findings still have teeth, because an
unresolved one blocks the stamp.

### Findings must not enter the agent's retry loop

`normalize_dld` is gated on `check_normalization` and nothing else. Routing
findings into that loop would put a non-deterministic verdict inside
`max_attempts`, where a reviewer that flip-flops burns the attempts and reports a
defect that does not exist. This repo has already paid for that lesson once: the
normalization stamp was briefly inside the agent's gate, which made the agent's
loop unwinnable by construction. Findings block the **stamp**, not the loop.

---

## 4. What it reviews

Not free-form critique — a checklist of the ways a faithful-looking
normalization can still be wrong:

| Class | The failure |
| --- | --- |
| `timing_attachment` | A stated delay attached to the wrong FSM or operation. The headline case; nothing mechanical catches it. |
| `wait_model_attachment` | A `Wait model:` block on the wrong interface, or a mode that does not match the blocking language it was derived from. |
| `state_semantics` | A state name preserved while the behaviour described for it changed. |
| `command_effect` | A command's effect, error condition, or ordering claim altered in the reshape. |
| `misplaced_unplaced` | Content parked under `## Unplaced Source Content` that had a perfectly good home, which quietly removes it from extraction. |
| `open_item_drift` | A DLD Open Item whose recorded resolution in `decisions/<ip>.md` does not match what the template actually says. |

The vocabulary is closed and enforced by the gate. An unknown class fails the
file rather than being ignored, so the reviewer cannot invent a category to make
a vague observation fit.

---

## 5. The findings file

`reviews/<ip>.findings.yaml`, tracked in git — a finding is a claim about the
repo's correctness and must survive until it is resolved, and be visible in the
pull request that resolves it. The gaps report taught this lesson: durable
content in a regenerated, gitignored file is content that disappears.

```yaml
ip: sram_ctrl_ip
source_sha256: 679ca42b...      # the pair this review was performed against
normalized_sha256: 421a5595...
findings:
  - id: F1
    class: timing_attachment
    severity: high              # high | medium | low
    claim: The 5-cycle scrub entry read is attached to the bank scheduler.
    source: "6.0 Timing, row 'Scrub entry read'"
    normalized: "6. FSM Timing Model, Bank Scheduler FSM row"
    why: The source attributes that cost to the scrub process; the model would
      charge it to every bank access.
```

The hash pair is the same device the normalization stamp uses, for the same
reason: a review is a statement about two specific documents. Edit either and the
review is stale, and the gate says so rather than letting an old clean review
vouch for new text.

Dismissal is a line in `decisions/<ip>.md` naming the finding:

```markdown
- **F1 dismissed:** the scrub reads through the same bank port, so the cost is
  charged where the contention occurs. Confirmed against §4.3.
```

That puts the human's reasoning in the tracked file that already exists for
exactly this, rather than inventing a second place for it.

---

## 6. Harness YAML

```yaml
  - name: review_normalization
    kind: agent
    command: external_agent_or_manual_edit
    required: false
    when: src_dld_exists
    gates: [check_review_findings]
    max_attempts: 1
    notes: >
      Adversarial read of the normalization against its source, looking for claims
      whose meaning changed while their tokens survived. Writes findings to
      reviews/{ip}.findings.yaml. It may fail a normalization and may never pass
      one: an empty findings list means nothing was found, not that the document
      is faithful.
  - name: check_review_findings
    command: python tools/check_review_findings.py {ip}
    required: true
    when: src_dld_exists
    awaiting_human: true
    notes: >
      Deterministic — every recorded finding must be fixed (which invalidates the
      review) or dismissed by name in decisions/{ip}.md. Pauses as *awaiting*
      rather than failing, because resolving a finding is a human step.
```

`max_attempts: 1`, deliberately: re-invoking a reviewer until it stops finding
things is how a fail-only check gets talked out of its findings.

---

## 7. What this costs

- **One more LLM invocation** per normalized IP, and a third agent contract to
  maintain.
- **False positives spend human attention.** That is the price of fail-only, and
  it is the right side to err on — but a reviewer with a poor precision rate will
  be ignored, which is worse than not having one.
- **A new tracked artifact** per reviewed IP.
- **It cannot prove absence of error.** It narrows the residual risk; it does not
  close it. The stamp still belongs to a person, and this proposal does not
  reduce what that person is signing — it only gives them a better-informed
  starting point.

---

## 8. Build order

1. **The deterministic half first, with no LLM at all.** ✅ **Done.**
   `tools/check_review_findings.py`, the file format, the closed class
   vocabulary, staleness detection, and dismissal parsing — proved against
   hand-written findings files. Same discipline as the extractor's calibration
   and the normalization gate: build the thing that judges before the thing that
   is judged.
2. **A mutation test for the reviewer itself.** ✅ **Done, and it earns its cost.**
   `Scrub entry read: 5 cycles` was moved from the ECC Scrub FSM's row to the
   Bank Scheduler's in a copy of the real `sram_ctrl_ip` pair. The mechanical
   gate passes that mutation — every token conserved, nothing invented — so the
   model would silently charge a scrub delay to every bank access.

   | Pair | Findings |
   | --- | --- |
   | mutated | 2 — the injected timing (`timing_attachment`, high, citing both documents) **and** the Figure 3 observation below |
   | control (the real, stamped pair) | 1 — the Figure 3 observation only |

   The delta is exactly the mutation: caught when present, not reported when
   absent. That is the property the stage lives or dies on, and it was measured
   rather than assumed.

   The repeated finding is not noise. Both runs observed that the source's
   `8.0 Still open` section ends with a sentence about Figure 3 needing reissue,
   and that normalization filed it under `## Unplaced Source Content` instead of
   `## Open Items` — so it never reaches the gaps report, and a reviewer reading
   the four remaining bullets would think the list complete. Whether that is a
   defect or a judgement call is exactly the kind of question the dismissal
   mechanism exists for.

   Caveat worth keeping: one mutation, one IP, one model. It establishes that the
   signal exists, not its rate. A larger mutation set — wait models on the wrong
   interface, an altered error condition — is the way to learn precision, and is
   cheap to run now that the harness exists (`reports/reviewer_experiment/`).
3. **Write `agents/normalization_reviewer_agent.md`** — the §2 asymmetry, the §4
   checklist, the file format, and a standing instruction to report nothing when
   nothing is found.
4. **Manual mode, then agent-driven** — wire the stage with no profile so it
   writes a prompt and pauses, review one normalization by hand against the gate,
   then let `--agent` drive it.

Step 1 is the one that de-risks the rest, and it is useful on its own: it makes a
finding a first-class, trackable object even when the finding came from a person.
