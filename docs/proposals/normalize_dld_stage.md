# Proposal: a `normalize_dld` stage ahead of `parse_dld`

**Status:** built. Steps 1–4 of the build order in §7 are done: the fidelity
gate, the agent contract, the stage (drivable by `--agent <profile>` or left in
manual mode), the docx path, and the stamp on the promotion path. Only step 5 —
the adjacent opportunities that reuse this stage's shape — is untouched. The
sections below describe the design as built, with the places reality diverged
from the sketch marked **[revised]**.

**Problem it addresses:** the extractor (`tools/dld_to_template.py`) is
deterministic and best-effort. It reads a specific set of markdown conventions
(documented in
[dld_extraction_rules.md](../../skills/ip-model-generation/references/dld_extraction_rules.md))
and degrades to `TODO_REVIEW` when they're absent. That determinism is a feature
— it's what makes extractor calibration a regression test — but it means a DLD
written in some other shape extracts badly, and a human has to hand-reshape it
before the pipeline is useful. Today the "drop a DLD, run one command" promise
only really holds for DLDs already written in-shape.

**What this stage does:** an LLM stage that rewrites an arbitrary DLD into the
shape the extractor expects — *without adding, removing, or altering any
engineering claim* — placed ahead of `parse_dld` and gated by a deterministic
fidelity check.

---

## 1. Where it sits

```text
watch dlds/                     (content-hash change detection)
  -> docx -> markdown           (existing, python-docx)
  -> normalize_dld              ★ NEW — agent stage, gated
  -> parse_dld                  (unchanged, deterministic)
  -> complete_template         (existing agent stage)
  -> check_dld_coverage / lint_template
  -> ... rest of the pipeline unchanged
```

The key property: **`normalize_dld` is upstream of every existing gate and
replaces none of them.** The extractor stays exactly as deterministic as it is
today; it simply gets fed a document it can read. If normalization is bad,
extraction degrades to `TODO_REVIEW` and the strict coverage gate stops
promotion — the same failure path as a badly-written DLD today. No trust is
spent.

### Why not "just use an LLM extractor"

Because the extractor's trustworthiness is currently a *proved* property:
re-parsing every golden DLD must reproduce its template's FSM name set exactly,
and that equality runs as a regression test. An LLM extractor trades that proof
for plausibility. Normalizing *into* the deterministic parser keeps the proof
and still absorbs format variation. This distinction is the whole design.

---

## 2. The two-file model

Normalization must never be able to quietly destroy the engineer's original
document. So it doesn't edit in place:

| File | Role | Tracked |
| --- | --- | --- |
| `dlds/<ip>_dld.src.md` | The author's original, in whatever shape they wrote it (or the docx conversion output) | yes — this is what the human maintains |
| `dlds/<ip>_dld.md` | The normalized, extractor-shaped document | yes — reviewed like generated code |
| `reports/<ip>.normalize.md` | What the stage changed, and every claim it could not place | no (gitignored, like other reports) |

`<ip>_dld.md` remains the pipeline's input, so **every downstream stage,
including the extractor and `check_template_coverage`, is untouched.** A DLD
that's already in-shape needs no `.src.md` at all — the stage is skipped
entirely (see the skip condition in §5), which means today's 11 DLDs are
unaffected.

The docx path composes cleanly: `ensure_markdown_dld()` already converts
`<ip>_dld.docx` to markdown. That conversion output becomes the `.src.md`, and
normalization runs on it.

---

## 3. The contract (what the agent may and may not do)

This is the part that makes the stage safe. It would live at
`agents/dld_normalization_agent.md`, written in the same register as
[`ip_model_generation_agent.md`](../../agents/ip_model_generation_agent.md).

### Permitted — *shape* changes only

- Retitle and renumber headings to `### <n.m> <Name> Interface` /
  `### <n.m> <Name> FSM`.
- Convert prose or a table listing states into a `States:` bullet list.
- Convert scattered timing prose into the
  `| FSM/process | Runs as | Delay model |` table, preserving each stated
  `"<op>: <c> cycles = <n> ns"` phrase verbatim in meaning.
- Restructure an interface's stated blocking behavior into a `Wait model:`
  bullet block **only when the source states it** (see below).
- Move an existing statement to the section where the extractor looks for it.
- Add a `Total FSM/processes: <N>` line **only** when N is derivable by counting
  FSM sections the source itself defines.
- Collect the source's open questions under an `## Open Items` heading.

### Forbidden — every *content* change

- Do not add an FSM, state, interface, command, queue, or timing number the
  source does not state.
- Do not remove or merge any FSM, state, or interface the source states.
- Do not resolve an ambiguity. An ambiguity is an **Open Item**, not a decision.
- Do not invent a wait model. If the source doesn't state how a requester waits,
  **leave the block out** — the extractor will stamp `assumed_default`, the gaps
  report will call it out, and `--strict` will refuse promotion. That is the
  correct outcome: it sends the engineer back to the DLD, which is exactly the
  behavior the system already promises.
- Do not change any number, unit, or name. Renaming for style is a content
  change.
- Do not delete content it cannot place — see the Unplaced section below.

### The Unplaced section (the escape hatch that prevents silent loss)

Anything in the source the agent cannot map to a known convention is copied
**verbatim** into a trailing section:

```markdown
## Unplaced Source Content

> Content preserved from <ip>_dld.src.md that did not map to an extractor
> convention. Review and either place it or confirm it is out of scope.

- §7.3 "Retry behaviour is under discussion with the fabric team."
- Figure 4 caption: "credit return path (informative)"
```

The extractor ignores this section, so it costs nothing downstream. Its purpose
is that **normalization is never lossy without saying so.** The fidelity gate
(§4) counts it.

---

## 4. The gate: `tools/check_dld_normalization.py`

An agent stage in this repo is only as good as the deterministic tool that
judges it. This is the new gate, and it is **entirely mechanical — no LLM.**

It compares `<ip>_dld.src.md` against `<ip>_dld.md` and fails on:

| Check | Rationale |
| --- | --- |
| **Number conservation** | Every numeric literal with a unit in the source (`2 ns`, `500 MHz`, `1 cycle`, `depth 8`) must appear in the normalized file with the same value and unit. A dropped or altered number is the most dangerous possible failure. |
| **Identifier conservation** | Every `ALL_CAPS_STATE`, backticked identifier, and register name in the source must survive. |
| **No net-new identifiers** | An `ALL_CAPS` state or `<fsm>.<STATE>` in the normalized file that appears nowhere in the source is invention — hard fail. This is the check that catches a hallucinated state. |
| **FSM-count parity** | If the source states `Total FSM/processes: N`, the normalized file must state the same N *and* contain N FSM sections. |
| **Wait-model provenance** | Every `Wait model:` block in the normalized file must be traceable to blocking language in the corresponding source section. A block with no source basis is invention — hard fail. |
| **Unplaced accounting** | Source paragraphs that appear in neither the normalized body nor the Unplaced section are silent loss — hard fail. |

Failures re-invoke the agent with the failure log, exactly like `unit_tests`
drives `agent_implementation` today.

### [revised] What normalizing a real document changed

The table above is what the gate checks. *How* it checks three of those rows had
to change once a whole document was reshaped by hand against it, rather than
mutated one line at a time. Each rule below was rejecting a transformation this
proposal explicitly permits:

- **Identifier conservation is markup-blind.** Comparing *marked* identifier sets
  meant an author who writes signal names in plain prose — the common case —
  failed with forty invented-name errors for a document in which every name was
  present. Invention now means the name appears in the source in no form at all.
- **Structural claims moved to where they can be checked properly.** Relaxing the
  above alone would have been a net weakening, so FSM state sets are now compared
  per FSM through the extractor. This closes part of the honest limit below: a
  state attached to the *wrong FSM* preserves every number and every name, and is
  now caught.
- **Headings are not content.** Retitling is the first permitted operation, so
  heading text cannot be held to conservation.
- **Unplaced accounting allows redistribution.** Requiring each source block to
  resemble a single normalized block rejected every prose-to-structure rewrite —
  which is the transformation the stage exists to perform. A redistributed block
  is now accepted when nearly all of its distinctive vocabulary survives
  somewhere; a deleted one still fails, because its words leave with it.
- **The wait-mode names are vocabulary, not claims.** `wait_for_ack_inline` is
  not in a source that states blocking behavior in prose, so writing the `Mode:`
  line looked like invention. The three names are exempt from that check and
  governed by wait-model provenance instead.

The lesson is the one the build order was designed around: calibration against
identity rewrites proves the tokenizer does not false-positive, which is a weaker
claim than it sounds. The pair in `tests/fixtures/normalization/` is the document
that found all five, and it is now a regression test.

### The honest limit of this gate

Token conservation proves *nothing was dropped or invented*. It does **not**
prove the meaning survived — an agent could faithfully preserve every number
while attaching it to the wrong FSM. That is a real residual risk and the
proposal should not pretend otherwise.

**[revised]** For states specifically, this is no longer entirely true — see
the state-set parity check above. The limit still stands for everything else, a
timing number attached to the wrong FSM being the obvious remaining case.

The repo already has the right pattern for exactly this class of claim: the
**provenance stamp**. `check_model_provenance.py --stamp <ip>` exists because
"this model was amended against this template revision" is a claim a hash
comparison cannot verify, so a human earns it per IP. Normalization needs the
same treatment:

```powershell
python tools\check_dld_normalization.py --stamp <ip>   # "I read the diff; the meaning survived"
```

The stamp records a hash pair (`src`, `normalized`) in
`dlds/normalization_baselines.json`. Editing either file breaks the stamp and
re-requires review. **A normalized DLD without a current stamp cannot promote a
template** — that's the human-judgment gate, and it is deliberately not
automatable. Consistent with `--stamp-all` on provenance, a bulk stamp should
refuse once baselines exist.

This keeps the system's core promise intact: the machine proves what it can
prove, and a human explicitly signs the one claim it cannot.

---

## 5. Harness YAML

**[revised]** *Three* stages, not two — see step 4 in §7 for why the gate had to
split. The sketch below is kept as written; `harness/ip_generation_loop.yaml` is
the built version. The runner gained one new placeholder, `{src_dld}`, one new
`when:` condition, `src_dld_exists`, and one new stage flag, `awaiting_human`:

```yaml
  - name: normalize_dld
    kind: agent
    command: external_agent_or_manual_edit
    required: false
    when: src_dld_exists
    gates: [check_normalization]
    max_attempts: 2
    notes: >
      Rewrite {src_dld} into the extractor-shaped {dld}, changing structure only —
      never adding, removing, or altering an engineering claim. Content that maps
      to no convention goes verbatim under "## Unplaced Source Content". Skipped
      when the normalized DLD already passes the fidelity gate.
  - name: check_normalization
    command: python tools/check_dld_normalization.py {ip}
    required: true
    when: src_dld_exists
    notes: >
      Deterministic fidelity gate — number/identifier conservation, no net-new
      identifiers, FSM-count parity, wait-model provenance, unplaced accounting.
      Also fails when the human normalization stamp is missing or stale.
```

Both carry `when: src_dld_exists`, so **an in-shape DLD with no `.src.md`
bypasses the stage entirely** — today's 11 DLDs and their gates are completely
unaffected. This is the same "skip when unnecessary" discipline the existing
agent stages use (`if the gates already pass, the agent is skipped`).

Change detection also needs `.src.md` in its hash set so editing the source
re-triggers normalization; `state_key`/`sha256` in `auto_ip_pipeline.py` already
generalize to this. **[revised]** Built as `source_fingerprint()`, which hashes
the DLD and its `.src.md` together — the `.src.md` is deliberately *not* added to
DLD discovery, since an author's source is an input to the stage, not an IP of
its own.

---

## 6. What this costs

Honest accounting, since the repo's style is to state limits plainly:

- **A new agent stage** — more LLM invocations per new IP, and a second contract
  document to maintain.
- **A genuinely new gate to write and test** — `check_dld_normalization.py` is
  the real work here; the tokenizing rules for "numeric literal with a unit" and
  "identifier" need care, and the wait-model provenance check is the subtlest.
- **A second human review point** — the normalization stamp. This is a real
  cost, deliberately accepted: it is what keeps the fidelity claim honest.
- **Two DLD files per IP** to hold in your head, for IPs that need normalization.

**What it buys:** the pipeline accepts DLDs as engineers actually write them,
which is the difference between a tool the team adopts and a tool that requires
authors to learn a markdown dialect first. It also makes the top-row opportunity
in the LLM discussion real: the biggest human bottleneck today is hand-shaping
documents, not generating models.

---

## 7. Suggested build order

1. ~~**`check_dld_normalization.py` first, with no agent at all.**~~ **Done.**
   Calibrated against the existing 11 DLDs, each file as its own source.
2. ~~**Write `agents/dld_normalization_agent.md`**~~ **Done.** The §3 contract,
   plus the gate's tokenizer rules an author has to work with — that
   `5,000,000` keeps its commas, that `1 cycle` and `1 cycles` are different
   values, and that paraphrasing is what unplaced accounting measures.
3. ~~**Manual-mode stage**~~ **Done.** `normalize_dld` and `check_normalization`
   are wired ahead of `parse_dld`, both `when: src_dld_exists`, so an in-shape
   DLD skips them entirely. With no `--agent` profile the runner writes the
   prompt to `reports/agent_requests/` and reports the IP as *awaiting*.
   `tests/fixtures/normalization/` holds the off-shape DLD normalized by hand
   against the gate, and the five rule changes that exercise produced are
   recorded above.
4. ~~**Then let an agent drive it**, with `--agent <profile>`, and add the
   normalization stamp to the promotion path.~~ **Done**, and both open questions
   were answered yes:
   - **The docx conversion output becomes the `.src.md`.** Word offers no way to
     write the extractor's conventions, so treating a converted document as
     already-in-shape was always optimistic. Conversion is deterministic and
     skips the write when the content is unchanged, so the stamp survives a
     re-run.
   - **`--strict` hard-requires the stamp.** Promotion is where the template
     becomes the source of truth for generation, so it is the right place to
     require the one claim the machine cannot make — and it holds even when
     someone runs the steps by hand, outside the pipeline.

   Wiring the agent surfaced a third question the sketch never asked: **what
   gates the agent?** The stage's gate was the full fidelity check, stamp
   included — so an agent would have normalized correctly, failed on a human's
   absence, retried, and failed again, reporting a defect where there was none.
   The gate is now split: the agent iterates against `--no-stamp-check`, and
   `check_normalization_stamp` is a separate required stage carrying a new
   `awaiting_human: true` flag, which reports the IP as *awaiting* rather than
   failed. A gate a machine cannot satisfy must not be in a machine's retry loop.
5. Only after that, consider the adjacent opportunities (gap-resolution
   proposer, gate-failure triage) — they reuse this stage's shape. **Not
   started.**

Step 1 is the one that de-risked everything else, and it was useful on its own.
Step 3 is what proved the gate: no LLM was involved in finding those five rule
defects, only a document reshaped by hand.

