# IP Model Generation Agent Contract

Use this contract for any human, LLM, or scripted agent that implements an IP
model from a prompt pack.

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
