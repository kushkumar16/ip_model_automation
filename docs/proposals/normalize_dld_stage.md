# Proposal: a `normalize_dld` stage ahead of `parse_dld`

**Status:** sketch for discussion — nothing in this document is wired into the
pipeline yet.

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
  -> review_template            (existing agent stage)
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

### The honest limit of this gate

Token conservation proves *nothing was dropped or invented*. It does **not**
prove the meaning survived — an agent could faithfully preserve every number
while attaching it to the wrong FSM. That is a real residual risk and the
proposal should not pretend otherwise.

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

Two stages, following the existing conventions (`kind: agent` + `gates:`,
`when:`, `{placeholder}` substitution). The runner would need one new
placeholder, `{src_dld}`, and one new `when:` condition, `src_dld_exists`:

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
generalize to this.

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

1. **`check_dld_normalization.py` first, with no agent at all.** Run it against
   the existing 11 DLDs using each file as its own source; it must pass
   trivially. That calibrates the tokenizer against real documents before any
   LLM output exists — the same discipline as extractor calibration.
2. **Write `agents/dld_normalization_agent.md`** (the §3 contract).
3. **Manual-mode stage**: wire `normalize_dld` as an agent stage with no
   profile, so it writes a prompt to `reports/agent_requests/` and pauses.
   Normalize one deliberately off-shape DLD by hand against the gate.
4. **Then let an agent drive it**, with `--agent <profile>`, and add the
   normalization stamp to the promotion path.
5. Only after that, consider the adjacent opportunities (gap-resolution
   proposer, gate-failure triage) — they reuse this stage's shape.

Step 1 is the one that de-risks everything else, and it's useful on its own.
