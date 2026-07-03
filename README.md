# IP Model Automation

This repo is a workflow for turning HW IP design-level documents into
model-ready templates, SimPy delay models, and unit tests.

The template is the source of truth for model generation. The DLD is used to
author and review the template, but generation should not infer missing
behavior from the DLD.

> **New here?** Read [docs/pipeline_overview.md](docs/pipeline_overview.md) for a
> diagrammed explanation of the whole flow and what each piece does.

## IPs

- `arbitration_ip`: hierarchical port/tenant/SQ arbitration with pending
  bitmaps, RR/WRR policy, and burst-limited issue pipeline.
- `completion_ip`: command completion scheduling with per-tenant QoS tokens,
  WWV write accounting, soft/hard burst behavior, and output backpressure.
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

DLDs vary in format and often omit details. Extract a draft template plus a gaps
report from a DLD:

```powershell
python tools\dld_to_template.py docs\<ip_name>_dld.md
```

This writes `templates\<ip_name>.template.draft.yaml` and
`reports\<ip_name>.gaps.md`. Replace every `TODO_REVIEW` marker using only
DLD-stated behavior, then check the draft covers the DLD and promote it:

```powershell
python tools\check_template_coverage.py templates\<ip_name>.template.draft.yaml docs\<ip_name>_dld.md --strict
```

Validate the whole flow (every DLD has a lint-passing, DLD-covering template,
then the model/test flow):

```powershell
python tools\validate_dld_flow.py
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

Generate an LLM prompt pack from a reviewed template:

```powershell
python tools\generate_prompt_pack.py templates\<ip_name>.template.yaml --output-dir prompt_packs
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

1. Write `docs/<ip_name>_dld.md`.
2. Extract a draft template and gaps report, then fill every `TODO_REVIEW`
   using only DLD-stated behavior:

   ```powershell
   python tools\dld_to_template.py docs\<ip_name>_dld.md
   ```

3. Check DLD coverage, then promote the draft to
   `templates\<ip_name>.template.yaml`:

   ```powershell
   python tools\check_template_coverage.py templates\<ip_name>.template.draft.yaml docs\<ip_name>_dld.md --strict
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

- `docs/*_dld.md`: human-readable IP design documents (template authoring source).
- `templates/*.template.yaml`: source of truth for model generation.
- `templates/*.template.draft.yaml`: extractor output awaiting review (gitignored).
- `reports/*.gaps.md`: per-IP missing-detail report from extraction (gitignored).
- `schemas/ip_model_template.schema.json`: machine-checkable template contract.
- `tools/dld_to_template.py`: DLD -> draft template + gaps report extractor.
- `tools/check_template_coverage.py`: DLD-coverage gate (template captures DLD FSMs).
- `tools/validate_dld_flow.py`: end-to-end DLD -> template -> model -> test gate.
- `tools/template_lint.py`: strict template contract checker.
- `tools/report_model_coverage.py`: FSM coverage/maturity report from templates.
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
