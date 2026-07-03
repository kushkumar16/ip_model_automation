# IP Model Automation

This repo is a workflow for turning HW IP design-level documents into
model-ready templates, SimPy delay models, and unit tests.

The template is the source of truth for model generation. The DLD is used to
author and review the template, but generation should not infer missing
behavior from the DLD.

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

## Flow

```text
DLD markdown
  -> reviewed IP template YAML
  -> template lint gate
  -> SimPy scaffold generation
  -> model implementation from template
  -> unit tests and validation
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
2. Create `templates/<ip_name>.template.yaml`.
3. Run:

   ```powershell
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

- `docs/*_dld.md`: human-readable IP design documents.
- `templates/*.template.yaml`: source of truth for model generation.
- `schemas/ip_model_template.schema.json`: machine-checkable template contract.
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
