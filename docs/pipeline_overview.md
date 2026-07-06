# IP Model Automation — Pipeline Overview

This document explains **what this project does** and **how the flow works**, end
to end, from a hardware IP design document to a validated SimPy performance model
with unit tests.

---

## 1. What we are doing

Hardware teams hand us **DLDs** (Design-Level Documents): human-written markdown
that describes an IP block — its interfaces, internal state machines (FSMs),
timing, and behavior. We need a **SimPy transaction-level delay model** and
**unit tests** for each IP.

Two problems make this hard:

1. **DLDs are inconsistent.** Every author formats things differently.
2. **DLDs omit details.** Command encodings, test scenarios, and exact invariants
   are often missing.

So we do **not** generate models directly from the DLD. Instead we introduce a
**template** as a normalized, machine-checkable *golden reference* in the middle:

```mermaid
flowchart LR
    DLD["📄 DLD markdown<br/>(inconsistent, incomplete)"]
    TPL["📐 Template YAML<br/>(golden reference)"]
    MODEL["🐍 SimPy model"]
    TEST["✅ Unit tests"]

    DLD -->|"extract + review"| TPL
    TPL -->|"generate + implement"| MODEL
    TPL -->|"scenarios"| TEST
    MODEL --- TEST

    classDef src fill:#e8f0fe,stroke:#4285f4;
    classDef gold fill:#fff4e5,stroke:#f5a623;
    classDef out fill:#e6f4ea,stroke:#34a853;
    class DLD src;
    class TPL gold;
    class MODEL,TEST out;
```

### The key rule: two sources of truth, one at a time

```mermaid
flowchart TB
    subgraph Authoring["Phase A — Authoring the template"]
        direction LR
        A_DLD["DLD is the source"] --> A_TPL["Template"]
    end
    subgraph Generation["Phase B — Generating the model"]
        direction LR
        B_TPL["Template is the source"] --> B_MODEL["Model + Tests"]
    end
    Authoring --> Generation

    note["The DLD is used ONLY to author/review the template.<br/>Once the template is reviewed, the model is built<br/>from the TEMPLATE alone — never re-reading the DLD.<br/>Missing details are surfaced, never silently invented."]
    Generation -.-> note
    classDef n fill:#fce8e6,stroke:#ea4335;
    class note n;
```

This separation is what keeps generated models trustworthy: a reviewer signs off
the template once, and everything downstream is a faithful function of it.

---

## 2. The end-to-end flow

```mermaid
flowchart TD
    DLD["📄 docs/&lt;ip&gt;_dld.md"]

    subgraph S0["Stage 0 — DLD → Reviewed Template"]
        EXTRACT["tools/dld_to_template.py"]
        DRAFT["templates/&lt;ip&gt;.template.draft.yaml<br/>+ reports/&lt;ip&gt;.gaps.md"]
        REVIEW["Human/LLM fills every TODO_REVIEW<br/>(only DLD-stated behavior)"]
        COVGATE{"check_template_coverage.py --strict<br/>+ template_lint.py"}
        TPL["📐 templates/&lt;ip&gt;.template.yaml"]
    end

    subgraph S1["Stage 1 — Template → Model + Tests"]
        SCAFFOLD["tools/generate_model_scaffold.py"]
        PACK["tools/generate_prompt_pack.py"]
        IMPL["Implement model + tests<br/>src/…/&lt;ip&gt;.py, tests/test_&lt;ip&gt;.py"]
        VALIDATE{"validate_dld_flow.py<br/>(lint · coverage · scaffold · tests)"}
        DONE["✅ Validated model + passing tests"]
    end

    DLD --> EXTRACT --> DRAFT --> REVIEW --> COVGATE
    COVGATE -->|fail| REVIEW
    COVGATE -->|pass| TPL
    TPL --> SCAFFOLD --> IMPL
    TPL --> PACK --> IMPL
    IMPL --> VALIDATE
    VALIDATE -->|fail| IMPL
    VALIDATE -->|pass| DONE

    classDef gate fill:#fff4e5,stroke:#f5a623;
    class COVGATE,VALIDATE gate;
```

### ASCII version (same flow)

```text
docs/<ip>_dld.md
      │
      ▼   Stage 0: DLD → Reviewed Template
 dld_to_template.py ── draft template + gaps report
      │
      ▼
 fill TODO_REVIEW  ◄──── (loop until gates pass)
      │
      ▼
 [gate] template_lint.py + check_template_coverage.py --strict
      │  pass
      ▼
 templates/<ip>.template.yaml   (golden reference)
      │
      ▼   Stage 1: Template → Model + Tests
 generate_model_scaffold.py + generate_prompt_pack.py
      │
      ▼
 implement src/<ip>.py + tests/test_<ip>.py  ◄──── (loop)
      │
      ▼
 [gate] validate_dld_flow.py  →  ✅ done
```

---

## 3. Stage 0 in detail — turning a messy DLD into a clean template

`tools/dld_to_template.py` is a **best-effort extractor**. It parses the DLD's
markdown conventions and fills what it can; anything it cannot derive is marked
`TODO_REVIEW` and listed in a **gaps report**.

```mermaid
flowchart LR
    subgraph DLD["DLD sections"]
        H1["## Purpose"]
        H2["### N.M … Interface"]
        H3["### N.M … FSM<br/>(States: …)"]
        H4["| FSM | Runs as | Delay model |"]
        H5["## Open Items"]
    end
    subgraph OUT["Extractor output"]
        F1["ip.description"]
        F2["interfaces[]"]
        F3["fsm_processes[] + states"]
        F4["timing_model (cycles/ns)"]
        F5["gaps report"]
        F6["TODO_REVIEW markers<br/>(commands, test_scenarios, invariants…)"]
    end
    H1 --> F1
    H2 --> F2
    H3 --> F3
    H4 --> F4
    H5 --> F5
    OUT --> F6
```

**What the extractor reliably gets:** FSM names + states, interface sections,
per-FSM timing operations, clock rate, and the FSM count.
**What it leaves for review:** commands, test scenarios, invariants, transition
detail, and any IP-specific contract fields.

### Why a gaps report instead of guessing

The gaps report (`reports/<ip>.gaps.md`) lists the DLD's own **Open Items** plus
every `TODO_REVIEW` field. This makes missing information *visible and tracked*
rather than silently filled with a plausible-but-wrong assumption.

---

## 4. The gates (what "validated" means)

Every stage is guarded by an automated gate. Nothing advances until its gate is
green.

```mermaid
flowchart TD
    G1["template_lint.py<br/>schema/contract shape"]
    G2["check_template_coverage.py --strict<br/>template captures every DLD FSM + count<br/>and has NO TODO_REVIEW"]
    G3["report_model_coverage.py<br/>every FSM has a test scenario"]
    G4["generate_model_scaffold.py<br/>scaffold shape (class + one process per FSM)"]
    G5["unit tests<br/>functional + timing assertions pass"]

    G1 --> G2 --> G3 --> G4 --> G5
    G5 --> OK["validate_dld_flow.py: OK"]

    classDef gate fill:#fff4e5,stroke:#f5a623;
    class G1,G2,G3,G4,G5 gate;
```

`tools/validate_dld_flow.py` runs the front-end gate for **every** DLD (template
exists, lints, covers the DLD) and then delegates to `validate_ip_flow.py` for the
model/test flow — a single command that proves the whole repo is consistent.

### Calibration — how we trust the extractor

The extractor was tuned against the existing golden IPs: **re-parsing each DLD
reproduces its template's FSM name set exactly**, and the DLD's stated
"Total FSM/processes: N" matches the template's `fsm_count`. That equality is the
regression gate — if a future change breaks extraction, calibration fails loudly.

> Interface sections are **report-only**, because templates legitimately
> consolidate them (e.g. a "Watchdog Heartbeat" DLD section folded into another
> template interface). FSM coverage is the hard gate; interfaces are informational.

---

## 5. Repository components

```mermaid
flowchart TB
    subgraph inputs["Inputs / sources"]
        DLDS["docs/*_dld.md"]
        SCHEMA["schemas/ip_model_template.schema.json"]
    end
    subgraph tools["tools/ (deterministic)"]
        T1["dld_to_template.py"]
        T2["template_lint.py"]
        T3["check_template_coverage.py"]
        T4["generate_model_scaffold.py"]
        T5["generate_prompt_pack.py"]
        T6["report_model_coverage.py"]
        T7["validate_ip_flow.py"]
        T8["validate_dld_flow.py"]
    end
    subgraph guidance["Guidance for humans/LLMs"]
        SK["skills/ip-model-generation/"]
        HR["harness/ip_generation_loop.yaml"]
        AG["agents/ip_model_generation_agent.md"]
    end
    subgraph outputs["Generated artifacts"]
        TPL["templates/*.template.yaml"]
        MOD["src/ip_model_automation/*.py"]
        TST["tests/test_*.py"]
    end

    DLDS --> T1 --> TPL
    SCHEMA --- T2
    TPL --> T2 & T3 & T4 & T5 & T6
    T4 --> MOD
    T5 --> MOD
    TPL --> MOD --> TST
    T8 --> T7
    SK -.guides.-> T1
    HR -.orchestrates.-> tools
    AG -.contract.-> MOD
```

| Area | Role |
| --- | --- |
| `docs/*_dld.md` | Human design intent (authoring source only). |
| `templates/*.template.yaml` | **Golden reference** — source of truth for generation. |
| `schemas/…schema.json` | Machine-checkable template contract. |
| `tools/*.py` | Deterministic extraction, linting, coverage, generation, validation. |
| `skills/…`, `harness/…`, `agents/…` | Instructions/contract for the human or LLM that fills templates, models, and tests. |
| `src/ip_model_automation/*.py` | Flat, one-file-per-IP SimPy delay models. |
| `tests/test_*.py` | Per-IP unit tests derived from `test_scenarios`. |

---

## 6. What a generated model looks like

Each template FSM becomes one SimPy process; queues/stores connect them. Example —
the **Mailbox IP** (one of the two IPs added through this pipeline):

```mermaid
flowchart LR
    SW(["Sender API<br/>send_message()"])
    RA["register_access"]
    MP["message_push"]
    MPOP["message_pop"]
    DB["doorbell"]
    IN["interrupt_notify"]
    RX(["Receiver<br/>read_message()"])
    IRQ(["IRQ out"])

    SW --> MP
    MP -->|message_fifo| MPOP --> RX
    MP -->|doorbell_queue| DB
    DB -->|interrupt_pending_queue| IN --> IRQ
    RA -.config/mask.-> MP & IN

    classDef p fill:#e8f0fe,stroke:#4285f4;
    class RA,MP,MPOP,DB,IN p;
```

- **Concurrency:** all five FSMs run as parallel `env.process(...)` loops.
- **Sequencing:** a message must be *enqueued* → *doorbell raised* → *interrupt
  asserted*; FIFO-full applies backpressure; masked channels suppress interrupts.
- **Timing:** every delay comes from the template's `timing_model`.
- **Observability:** IP-tagged logs (`[mailbox_ip] …`) and a `metrics` dict the
  unit tests assert on.

---

## 7. How to add a new IP (worked recipe)

```mermaid
sequenceDiagram
    participant U as Author
    participant X as dld_to_template.py
    participant R as Reviewer / LLM
    participant G as Gates
    participant V as validate_dld_flow.py

    U->>X: docs/<ip>_dld.md
    X-->>R: draft template + gaps report
    R->>R: fill TODO_REVIEW (DLD-stated only)
    R->>G: lint + coverage --strict
    G-->>R: fail → keep fixing
    G-->>R: pass → promote to golden template
    R->>R: write src/<ip>.py + tests/test_<ip>.py
    R->>V: run full flow
    V-->>U: ✅ all 11 IPs green, tests pass
```

Concretely:

1. Write `docs/<ip>_dld.md` (Purpose, Interfaces, FSMs+States, timing table, Open Items).
2. `python tools\dld_to_template.py docs\<ip>_dld.md` → draft + gaps.
3. Fill every `TODO_REVIEW`; resolve gaps using only DLD-stated behavior.
4. `python tools\check_template_coverage.py templates\<ip>.template.draft.yaml docs\<ip>_dld.md --strict`
   then promote to `templates\<ip>.template.yaml`.
5. Implement `src/ip_model_automation/<ip>.py` and `tests/test_<ip>.py`
   (register the model in `common.py` + `ip.py`).
6. `python tools\validate_dld_flow.py` → green.

This is exactly the path used to add `mailbox_ip`, `spi_master_ip`, and
`i3c_ip`: brand-new DLDs went through extraction, review, modeling, and
testing, and the whole 11-IP suite validated with 58 passing tests. The same
path also carries subsystems (`dma_subsystem`, `mailbox_irq_subsystem`):
connected clusters of member IPs whose glue processes are their FSMs and whose
member models are their resources, so every existing gate applies unchanged.

---

## 8. Summary

- We convert **inconsistent, incomplete DLDs** into **validated SimPy models**.
- A **normalized template** sits in the middle as the golden reference.
- **Deterministic tools** handle extraction, linting, coverage, and validation;
  a **human/LLM** fills the judgment gaps under a strict contract.
- **Missing details are surfaced** in a gaps report, never invented.
- **Every stage has an automated gate**, and one command
  (`validate_dld_flow.py`) proves the whole repo is consistent.
