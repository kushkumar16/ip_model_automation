# IP Model Generation Agent Contract

Use this contract for any human, LLM, or scripted agent that implements an IP
model from a prompt pack.

## Inputs

- Reviewed template: `templates/<ip_name>.template.yaml`
- Prompt pack: `prompt_packs/<ip_name>.prompt.md`
- Optional scaffold: `src/ip_model_automation/<ip_name>.py`
- Skill rules: `skills/ip-model-generation/SKILL.md`

## Responsibilities

1. Use the template and prompt pack as the only source of generation behavior.
2. Implement or repair `src/ip_model_automation/<ip_name>.py`.
3. Implement or repair `tests/test_<ip_name>.py`.
4. Preserve flat model layout.
5. Use SimPy process methods for template FSMs.
6. Use template timing for hard-coded delays.
7. Add assertions for functionality and performance effects from `test_scenarios`.
8. Run `python tools/validate_ip_flow.py`.

## Forbidden Actions

- Do not infer missing behavior from the DLD during implementation.
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
