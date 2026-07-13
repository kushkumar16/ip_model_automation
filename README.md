# IP Model Automation

This repo is a workflow for turning HW IP design-level documents into
model-ready templates, SimPy delay models, and unit tests.

The template is the source of truth for model generation. The DLD is used to
author and review the template, but generation should not infer missing
behavior from the DLD.

> **New here?** Read [docs/project_overview.md](docs/project_overview.md) — the
> single project document: intention, a diagrammed explanation of every
> pipeline stage, code conventions, and current status.

## Repository Layout

| Directory | Contents |
| --- | --- |
| `dlds/` | **Input DLDs** — the authoring source (`*_dld.md`; `*_dld.docx` also accepted by the automated pipeline). |
| `templates/` | Reviewed template YAMLs (source of truth for generation); extractor drafts land here too (gitignored). |
| `src/ip_model_automation/` | Flat SimPy model implementations. |
| `tests/` | Per-IP unit tests and workflow tests. |
| `reports/` | **All generated output** (gitignored): gaps reports, readable template docs (md + html), agent requests, experiment results, pipeline state. |
| `prompt_packs/` | Generated LLM prompt bundles (gitignored). |
| `docs/` | Project documentation: `project_overview.md` (single consolidated doc), `project_overview.docx` (Word version with embedded diagrams), `diagrams/` (standalone SVG flow diagrams, gallery at `diagrams/index.html`). |
| `tools/` | Pipeline tools: extraction, gates, generators, validation, automation. |
| `schemas/`, `examples/` | Template contract schema and authoring examples. |
| `skills/`, `harness/`, `agents/` | LLM generation skill, loop stages/pass criteria, agent contract. |

## IPs

- `arbitration_ip`: hierarchical port/tenant/SQ arbitration with pending
  bitmaps, RR/WRR policy, and burst-limited issue pipeline.
- `completion_ip`: command completion scheduling with per-tenant QoS tokens,
  window-based refill, and output backpressure.
- `gdma_ip`: descriptor-driven DMA with multiple parallel/sequential FSMs.
- `timer_ip`: register, tick, compare, watchdog, debug-freeze, and interrupt
  timer behavior.
- `axi_interconnect_ip`: AXI read/write routing, arbitration, response routing,
  and decode-error handling.
- `interrupt_controller_ip`: source sampling, pending/filter/priority,
  delivery, ACK, EOI, and software interrupt behavior.
- `mailbox_ip`: multi-channel inter-processor messaging with per-channel
  FIFOs, doorbell generation, and masked interrupt aggregation.
- `spi_master_ip`: SPI master with TX/RX byte FIFOs, a bit-shift transfer
  engine, chip-select framing, and masked interrupts.
- `i3c_ip`: I3C master with a queued command engine, bit-level SDR transfer
  engine, in-band-interrupt (IBI) detection/arbitration, and masked
  interrupts.

## Subsystems

A subsystem is modeled as "just another IP" whose FSM processes are the glue
between member IP models; the member models are its resources. It rides the
same DLD -> template -> model -> tests pipeline and gates.

- `dma_subsystem`: connected data-mover composing `gdma_ip`,
  `axi_interconnect_ip`, `arbitration_ip`, and `completion_ip`. Descriptors
  fan out to an engine leg and a fabric leg (read+write commands routed,
  arbitrated, and QoS-accounted); the subsystem IRQ fires when both legs
  complete. A backpressure monitor couples fabric congestion to GDMA memory
  readiness and completion backlog to arbitration issue readiness.
- `mailbox_irq_subsystem`: interrupt-delivery cluster composing `mailbox_ip`
  (doorbell source) and `interrupt_controller_ip` (delivery fabric) with a
  modeled software service loop (ack, read, clear, EOI). Uses true
  level-triggered semantics — the doorbell level stays asserted while
  unserviced messages remain, so EOI re-pends the source — and duty-cycle
  interrupt-storm throttling that masks a flooding channel for a throttle
  window.

## Flow

```text
DLD markdown
  -> dld_to_template extraction  (draft template + gaps report)
  -> review/fill TODO_REVIEW     (only DLD-stated behavior)
  -> DLD coverage + lint gate    (promote to reviewed template)
  -> SimPy scaffold generation
  -> model implementation from template
  -> unit tests and validation
```

The DLD is the only source for *authoring* the template; the reviewed template
is the source of truth for *model generation*. Missing DLD details are surfaced
in a gaps report, never silently invented.

## DLD To Template

New to template authoring? Start from the annotated reference
[templates/reference_template.yaml](templates/reference_template.yaml) — it
documents every section, the rules each gate enforces, and the optional
`subsystem:` section. It is kept lint-clean by the test suite but is named so
template discovery never treats it as a real IP.

DLDs vary in format and often omit details. Extract a draft template plus a gaps
report from a DLD:

```powershell
python tools\dld_to_template.py dlds\<ip_name>_dld.md
```

This writes `templates\<ip_name>.template.draft.yaml`,
`reports\<ip_name>.gaps.md`, and a readable HTML view of the draft at
`reports\template_docs\<ip_name>.template.draft.html`. Replace every
`TODO_REVIEW` marker using only DLD-stated behavior, then check the draft
covers the DLD and promote it:

```powershell
python tools\check_template_coverage.py templates\<ip_name>.template.draft.yaml dlds\<ip_name>_dld.md --strict
```

Validate the whole flow (every DLD has a lint-passing, DLD-covering template,
subsystem wiring is consistent, then the model/test flow). When the gate
passes it also regenerates the readable Markdown + HTML docs for every
promoted template in `reports\template_docs\`:

```powershell
python tools\validate_dld_flow.py
```

Check subsystem wiring only (members exist, member APIs and glue FSMs
referenced by `connections:` are real, the model instantiates the members):

```powershell
python tools\check_subsystem_wiring.py
```

## Automated Pipeline

`tools/auto_ip_pipeline.py` watches `dlds/*_dld.md` (and `*_dld.docx`) for new
or modified DLDs and executes the stage sequence declared in
`harness/ip_generation_loop.yaml` for each changed IP: docx -> markdown
conversion (python-docx), draft extraction, template gates, scaffold (new IPs
only), prompt pack, model/test generation, unit tests, and the repo-wide
validation gate. The harness YAML is the single source of truth for the
pipeline — adding, removing, or reordering a stage is a YAML edit, not a
runner change (the stage schema is documented at the top of that file).
Change detection hashes DLD content into `reports/.dld_pipeline_state.json`;
an IP is only marked processed after its full chain passes.

```powershell
python tools\auto_ip_pipeline.py                      # process every changed DLD
python tools\auto_ip_pipeline.py dlds\my_ip_dld.docx  # process one DLD explicitly
python tools\auto_ip_pipeline.py --force              # reprocess everything
```

The two harness agent stages (`review_template`, `agent_implementation`) need
an LLM or a human. By default the runner writes a ready-to-send prompt to
`reports\agent_requests\<ip>.<stage>.prompt.md` and reports the IP as
*awaiting* that stage. To run them unattended, pass an agent command — the
prompt is piped to its stdin, and failed validations re-invoke the agent with
the failure log up to `loop_policy.max_iterations` from the harness:

```powershell
python tools\auto_ip_pipeline.py --agent-cmd "claude -p --permission-mode acceptEdits"
```

## Daily Validation

Run the full flow:

```powershell
python tools\validate_ip_flow.py
```

This command:

- lints every `templates/*.template.yaml` file
- smoke-generates a scaffold for every IP in a temporary directory
- verifies each scaffold has the expected model class and FSM process methods
- checks template FSM coverage from `test_scenarios`
- runs the unit test suite

Run only the FSM coverage report:

```powershell
python tools\report_model_coverage.py
```

Check coding style — ruff lint + format over `src/`, `tools/`, and `tests/`
(rules in `ruff.toml`, conventions documented in
`skills/ip-model-generation/references/coding_style.md`; the automated
pipeline runs this as a hard repo-wide gate):

```powershell
python tools\check_code_style.py          # check only
python tools\check_code_style.py --fix    # auto-fix and reformat
```

Measure *code* coverage — which model lines the unit tests actually execute
(distinct from the template FSM/scenario coverage above). The table lands in
`reports\code_coverage\coverage.txt`; `--html` adds a browsable report at
`reports\code_coverage\html\index.html`, and `--fail-under N` (total) /
`--fail-under-file N` (each model file) turn it into a gate — the automated
pipeline enforces 95% for both:

```powershell
python tools\run_code_coverage.py
python tools\run_code_coverage.py --html --fail-under 95 --fail-under-file 95
```

Generate an LLM prompt pack from a reviewed template:

```powershell
python tools\generate_prompt_pack.py templates\<ip_name>.template.yaml --output-dir prompt_packs
```

Render templates into human-readable documents — Markdown by default, or
standalone HTML pages that open in any browser (output lands in
`reports/template_docs/`, gitignored; use `--stdout` to print one to the
terminal):

```powershell
python tools\render_template_doc.py                                  # all reviewed templates (Markdown)
python tools\render_template_doc.py --format html                    # styled HTML pages
python tools\render_template_doc.py --format both                    # both formats
python tools\render_template_doc.py templates\timer_ip.template.yaml --stdout
```

Inspect the generation harness:

```powershell
python tools\inspect_harness.py
```

Run deterministic loop validation:

```powershell
python tools\run_loop_validation.py
```

For scaffold-only validation:

```powershell
python tools\validate_ip_flow.py --skip-tests
```

## Performance Experiments

Sweep model configurations and extract latency/throughput tables from the
models' metrics (results land in `reports/experiments/`, gitignored):

```powershell
python tools\run_experiments.py            # run all experiments
python tools\run_experiments.py --list     # list available experiments
python tools\run_experiments.py --only dma_outstanding_limit
```

Each experiment sweeps one parameter against a fixed deterministic workload —
e.g. descriptor end-to-end latency vs. interconnect outstanding limit,
completion delay vs. QoS token budget, message round-trip vs. storm throttle
window, issued commands vs. burst credit. Output is one CSV per experiment
plus a combined `summary.md` with markdown tables.

## Model Logging

Every IP model accepts `log_level` and `log_file` constructor arguments. Logs
are written to the terminal and appended to `run.log` by default. Supported
levels are `CRITICAL`, `ERROR`, `WARNING`, `INFO`, and `DEBUG`.

Log lines include the IP tag:

```text
2026-07-03 10:00:00,000 INFO [arbitration_ip] issued port=port0 tenant=T0 sq=SQ0 issue_count=1 cmds=['cmd0']
```

Model constructors default to `WARNING` to keep validation output compact. Use
`log_level="INFO"` or `log_level="DEBUG"` when running a trace/debug scenario.

## Adding A New IP

1. Write `dlds/<ip_name>_dld.md`.
2. Extract a draft template and gaps report, then fill every `TODO_REVIEW`
   using only DLD-stated behavior:

   ```powershell
   python tools\dld_to_template.py dlds\<ip_name>_dld.md
   ```

3. Check DLD coverage, then promote the draft to
   `templates\<ip_name>.template.yaml`:

   ```powershell
   python tools\check_template_coverage.py templates\<ip_name>.template.draft.yaml dlds\<ip_name>_dld.md --strict
   python tools\template_lint.py templates\<ip_name>.template.yaml
   ```

4. Generate the initial model scaffold:

   ```powershell
   python tools\generate_model_scaffold.py templates\<ip_name>.template.yaml --output-dir src\ip_model_automation
   ```

5. Fill the model behavior from the template only.
6. Add unit tests from `test_scenarios`.
7. Run:

   ```powershell
   python tools\validate_ip_flow.py
   ```

## Key Files

- `dlds/*_dld.md`: human-readable IP design documents (template authoring source).
- `templates/*.template.yaml`: source of truth for model generation.
- `templates/*.template.draft.yaml`: extractor output awaiting review (gitignored).
- `reports/*.gaps.md`: per-IP missing-detail report from extraction (gitignored).
- `schemas/ip_model_template.schema.json`: machine-checkable template contract.
- `tools/auto_ip_pipeline.py`: change-driven DLD -> template -> model -> tests runner.
- `tools/dld_to_template.py`: DLD -> draft template + gaps report extractor.
- `tools/check_template_coverage.py`: DLD-coverage gate (template captures DLD FSMs).
- `tools/validate_dld_flow.py`: end-to-end DLD -> template -> model -> test gate.
- `tools/template_lint.py`: strict template contract checker.
- `tools/report_model_coverage.py`: FSM coverage/maturity report from templates.
- `tools/run_code_coverage.py`: line coverage of the models from the unit tests (coverage.py).
- `tools/check_code_style.py`: coding-style gate (ruff lint + format; `--fix` to auto-repair).
- `ruff.toml`: lint/format rules; the prose conventions live in `skills/ip-model-generation/references/coding_style.md`.
- `tools/render_template_doc.py`: template -> human-readable Markdown/HTML renderer.
- `docs/project_overview.md`: the single project document (intention, stage-by-stage flow diagrams, conventions, current status).
- `tools/generate_model_scaffold.py`: SimPy scaffold generator.
- `tools/generate_prompt_pack.py`: LLM-ready prompt bundle generator.
- `tools/inspect_harness.py`: generation harness inspector.
- `tools/run_loop_validation.py`: deterministic harness/agent loop validator.
- `tools/validate_ip_flow.py`: unified validation command.
- `examples/model_generation_skill.md`: LLM generation instructions.
- `skills/ip-model-generation`: reusable Codex skill for template-driven IP model generation.
- `harness/ip_generation_loop.yaml`: generation-loop stages and pass criteria.
- `agents/ip_model_generation_agent.md`: contract for human/LLM/model-generation agents.
- `src/ip_model_automation/*.py`: flat SimPy model implementations.
- `tests/test_workflow.py`: registry, scaffold, and validation helper checks.
- `tests/test_<ip_name>.py`: per-IP SimPy model tests.
