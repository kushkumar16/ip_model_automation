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
7. Implement each interface's `wait_model` at the `<fsm>.<STATE>` its
   `wait_points` name, and publish that state through `fsm_state` while the
   process is parked there. A `wait_for_response` interface blocks until the
   response returns; `wait_for_ack_inline` blocks at the request site;
   `wait_for_ack_before_next_request` continues and collects the ack before the
   next request, with at most `outstanding_limit` in flight.
   `tools/check_wait_model_coverage.py` fails the build otherwise.
8. Add assertions for functionality and performance effects from `test_scenarios`.
9. Run `python tools/validate_dld_flow.py` (front-end gate + model/test flow).

### Stage 2: amending an existing model (brownfield)

When the IP already has a model and its DLD changed, the pipeline runs the
`amend_implementation` stage instead of Stage 1 and pipes you a **structured
template diff** (from `tools/diff_template.py`) rather than the full prompt
pack. The task is different: **edit in place, do not regenerate.**

1. Read the delta. Every change is tagged with a blast radius:
   - `SURGICAL` — a localized value or addition (queue depth, timing number, a
     new scenario). Make the minimal corresponding edit.
   - `STRUCTURAL` — a topology change (FSM, state, interface, or command
     added/removed). Check whether it cascades before editing.
2. Change only what the delta requires. Leave unrelated model and test code
   untouched — an unrelated rewrite is a contract violation, not a style
   preference.
3. Keep timing values, queue depths, and FSM structure consistent with the new
   template; add or update tests for changed/added scenarios and keep every FSM
   covered.
4. Run `python tools/validate_dld_flow.py`. The pipeline stamps the new
   provenance baseline (`tools/check_model_provenance.py --stamp <ip>`) once the
   gates pass — per IP, after the amend. The stamp asserts that this model was
   amended against this template revision, so never stamp to quiet the gate
   (`--stamp-all` refuses once baselines exist).

If the template change is trivial enough that the existing tests still pass, the
stage is skipped entirely — you will not be invoked.

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
- During an amend (Stage 2), do not rewrite the model or tests from scratch and
  do not change code the template delta does not touch.

## Completion Report

Return:

- changed files
- implemented FSMs
- implemented test scenarios
- validation command output summary
- any missing template fields that blocked implementation
