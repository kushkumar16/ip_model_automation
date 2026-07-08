---
name: ip-model-generation
description: Parse hardware IP DLDs into template YAML, then generate, review, or repair Python SimPy transaction-level delay models and unit tests from those templates. Use when the task mentions IP DLDs, DLD-to-template extraction, HW IP modeling, FSM/process modeling, SimPy delay/performance models, template-driven LLM generation, scaffold generation, prompt packs, or validation of generated IP models/tests.
---

# IP Model Generation

Use this skill when generating or validating SimPy models from HW IP DLDs. It
covers the full flow `dlds/<ip>_dld.md -> reviewed template -> model -> tests`.
If you already have a reviewed `*.template.yaml`, skip to the "Template ->
Model" workflow below.

## Stage 0: DLD -> Reviewed Template

DLDs vary in format and often omit details. Never invent missing behavior;
surface it in the gaps report.

1. Extract a draft template and a gaps report from the DLD:

   ```powershell
   python tools\dld_to_template.py dlds\<ip_name>_dld.md
   ```

   This writes `templates\<ip_name>.template.draft.yaml` and
   `reports\<ip_name>.gaps.md`.

2. Read `reports\<ip_name>.gaps.md`. Replace every `TODO_REVIEW` marker in the
   draft using only behavior the DLD states. Where the DLD leaves a detail open
   (see its "Open Items"), pick a conservative, clearly-labeled default and
   record it in the gaps report instead of silently inventing behavior.
3. Lint and check DLD coverage of the draft:

   ```powershell
   python tools\template_lint.py templates\<ip_name>.template.draft.yaml
   python tools\check_template_coverage.py templates\<ip_name>.template.draft.yaml dlds\<ip_name>_dld.md
   ```

4. When both pass and no `TODO_REVIEW` remains, promote the draft to the golden
   template `templates\<ip_name>.template.yaml`, then re-run
   `check_template_coverage.py ... --strict` on the promoted file.

Read `references/dld_extraction_rules.md` for the expected DLD conventions and
the "stated vs inferred" rule.

## Template -> Model Workflow

1. Treat the reviewed template as the source of truth.
2. Run template lint before model generation:

   ```powershell
   python tools\template_lint.py templates\<ip_name>.template.yaml
   ```

3. Generate a scaffold before implementation:

   ```powershell
   python tools\generate_model_scaffold.py templates\<ip_name>.template.yaml --output-dir src\ip_model_automation
   ```

4. Fill model behavior using only template fields.
5. Add or update `tests\test_<ip_name>.py` from `test_scenarios`.
6. Run the full validation flow:

   ```powershell
   python tools\validate_ip_flow.py
   ```

   To validate the DLD front-end as well (every DLD has a lint-passing,
   DLD-covering template, then the model/test flow):

   ```powershell
   python tools\validate_dld_flow.py
   ```

## Prompt Packs

When asked to prepare LLM input for another model, generate a prompt pack:

```powershell
python tools\generate_prompt_pack.py templates\<ip_name>.template.yaml --output-dir prompt_packs
```

Use the prompt pack as the generation request. Do not supplement it with
unstated behavior from the DLD.

## Rules

- Keep one flat model file: `src\ip_model_automation\<ip_name>.py`.
- Generate one class named `<CamelIpName>Model`.
- Generate one SimPy process per `fsm_processes` entry with `simpy_process: true`.
- Use only `timing_model.fsm_process_delays` for hard-coded delays.
- Preserve `fsm_relationships.sequential_paths` and parallel process structure.
- Implement interfaces, queues, resources, commands, metrics, and invariants from the template.
- Use `get_ip_logger("<ip_name>", log_level, log_file)` for IP-tagged logs.
- Every model constructor must accept `log_level: str = "WARNING"` and
  `log_file: str = "run.log"`.
- Emit useful `CRITICAL`, `ERROR`, `WARNING`, `INFO`, and `DEBUG` events for
  fatal events, invalid/error paths, stalls/backpressure, command lifecycle,
  and detailed FSM transitions.
- Do not create SystemC, C++ functional models, per-IP folders, `perf_model.py`, or `functional_model.py`.
- Do not leave scaffold TODO comments in finalized models.

For detailed implementation and test rules, read
`references/model_generation_rules.md`.
