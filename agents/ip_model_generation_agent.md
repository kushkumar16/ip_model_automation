# IP Model Generation Agent Contract

Use this contract for any human, LLM, or scripted agent that implements an IP
model from a prompt pack. The pipeline is agent-agnostic: models and tests may
be written by any vendor's coding agent (Claude, Codex, Gemini, an in-house
agent, ...) or by a human — the gates judge the output, not the author.

## Runtime Requirements (any vendor)

`tools/auto_ip_pipeline.py` drives agent stages by piping a self-contained
prompt to a shell command's stdin. To plug in, an agent command must:

- run **non-interactively** (headless / auto-approve mode — no prompts for
  permission or input),
- **read the prompt from stdin** (the prompt is also saved to
  `reports/agent_requests/<ip>.<stage>.prompt.md` for manual replay),
- be able to **create and edit files** under the repo root (model, tests,
  template),
- **exit when finished** — pass/fail is decided by the stage's gates
  (unit tests, lint, coverage), not by the agent's exit code; while a gate
  fails, the agent is re-invoked with the failure log appended, up to
  `max_attempts`.

Named commands live in `harness/ip_generation_loop.yaml` under
`agent_profiles` (select with `--agent <name>`); any other command can be
passed raw via `--agent-cmd "..."`. The prompt pack plus this contract are the
complete task description — an agent needs no other repo context, and the
coding style gate (`python tools/check_code_style.py`) holds every agent's
output to the same conventions.

## Inputs

- DLD: `dlds/<ip_name>_dld.md`
- Draft template + gaps report: `templates/<ip_name>.template.draft.yaml`,
  `reports/<ip_name>.gaps.md`
- Reviewed template: `templates/<ip_name>.template.yaml`
- Prompt pack: `prompt_packs/<ip_name>.prompt.md`
- Optional scaffold: `src/ip_model_automation/<ip_name>.py`
- Skill rules: `skills/ip-model-generation/SKILL.md` and
  `skills/ip-model-generation/references/dld_extraction_rules.md`

## Responsibilities

### Stage 0: DLD -> reviewed template

1. Generate the draft with `python tools/dld_to_template.py dlds/<ip_name>_dld.md`.
2. Replace every `TODO_REVIEW` in the draft using only DLD-stated behavior;
   record anything the DLD leaves open in the gaps report with a labeled default.
3. Pass `template_lint.py` and
   `check_template_coverage.py <draft> <dld> --strict`, then promote the draft to
   `templates/<ip_name>.template.yaml`.

### Stage 1: template -> model + tests

1. Use the template and prompt pack as the only source of generation behavior.
2. Implement or repair `src/ip_model_automation/<ip_name>.py`.
3. Implement or repair `tests/test_<ip_name>.py`.
4. Preserve flat model layout.
5. Use SimPy process methods for template FSMs.
6. Use template timing for hard-coded delays.
7. Add assertions for functionality and performance effects from `test_scenarios`.
8. Run `python tools/validate_dld_flow.py` (front-end gate + model/test flow).

## Forbidden Actions

- Do not invent template fields absent from the DLD; record them as gaps and
  leave/replace `TODO_REVIEW` deliberately, never silently.
- Do not promote a draft that still contains `TODO_REVIEW`.
- Do not infer missing behavior from the DLD during model implementation
  (Stage 1): stop if the promoted template lacks required behavior.
- Do not create SystemC or C++ functional models.
- Do not create per-IP model folders.
- Do not add `perf_model.py`, `functional_model.py`, or `soc_models.py`.
- Do not remove existing IPs from registry or validation.

## Completion Report

Return:

- changed files
- implemented FSMs
- implemented test scenarios
- validation command output summary
- any missing template fields that blocked implementation
