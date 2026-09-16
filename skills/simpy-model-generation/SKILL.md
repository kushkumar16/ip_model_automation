---
name: simpy-model-generation
description: Generate, amend, or validate Python SimPy transaction-level delay models and unit tests from a reviewed IP template.yaml. Use when the task mentions SimPy delay/performance models, FSM/process modeling in Python, template-driven model generation, scaffold generation, prompt packs for a SimPy model, or validating a generated model/tests against its template. Requires an already-reviewed template.yaml — see the dld-to-template skill to produce one from a DLD first.
---

# SimPy Model Generation

Use this skill once a DLD has been turned into a reviewed
`templates/<ip>.template.yaml` (see the `dld-to-template` skill if it hasn't
been yet). It covers `reviewed template -> SimPy model -> unit tests ->
validation`, plus amending an existing model and generating prompt packs for
another agent.

## Template -> Model Workflow

1. Treat the reviewed template as the source of truth.
2. Run template lint before model generation:

   ```shell
   python tools/template_lint.py templates/<ip_name>.template.yaml
   ```

3. Generate a scaffold before implementation:

   ```shell
   python tools/generate_model_scaffold.py templates/<ip_name>.template.yaml --output-dir src/ip_model_automation
   ```

4. Fill model behavior using only template fields — including each
   interface's `wait_model`: the process must park at the `<fsm>.<STATE>`
   its `wait_points` name and publish that state through `fsm_state`
   (`python tools/check_wait_model_coverage.py` is the gate).
5. Add or update `tests/test_<ip_name>.py` from `test_scenarios`.
6. Run the full validation flow:

   ```shell
   python tools/validate_ip_flow.py
   ```

   To validate the DLD front-end as well (every DLD has a lint-passing,
   DLD-covering template, then the model/test flow):

   ```shell
   python tools/validate_dld_flow.py
   ```

## Amending An Existing Model (DLD Changed)

When the IP already has a model, do **not** regenerate it. Diff the template
(not the prose DLD — the template is normalized, so its diff is a clean
typed change list) and make only the corresponding edits:

```shell
git show HEAD:templates/<ip_name>.template.yaml > old.yaml
python tools/diff_template.py old.yaml templates/<ip_name>.template.yaml
python tools/diff_template.py old.yaml templates/<ip_name>.template.yaml --amend-prompt --ip <ip_name>
```

Each change is tagged `SURGICAL` (localized value/addition — edit in place)
or `STRUCTURAL` (FSM/state/interface/command added or removed — check for
cascade first). After the edits pass `validate_dld_flow.py`, refresh the
baseline:

```shell
python tools/check_model_provenance.py --stamp <ip_name>
```

`python tools/check_model_provenance.py` (no args) reports any template that
has drifted from the model baseline it was last built against. Stamp per IP
and only after the amend: the stamp asserts the model was amended against
that revision, which the hash itself cannot verify (`--stamp-all` refuses
once baselines exist, since it would make that claim for every IP at once).
The automated pipeline runs all of this for you as the `amend_implementation`
stage.

## Prompt Packs

When asked to prepare LLM input for another model, generate a prompt pack:

```shell
python tools/generate_prompt_pack.py templates/<ip_name>.template.yaml --output-dir prompt_packs
```

Use the prompt pack as the generation request. Do not supplement it with
unstated behavior from the DLD.

## Rules

- Keep one flat model file: `src/ip_model_automation/<ip_name>.py`.
- Generate one class named `<CamelIpName>Model`.
- Those paths and that class-naming rule come from `target_profile.yaml` (see
  `tools/target_profile.py`); the values above are this repo's profile. If you
  are driving another codebase, read its profile rather than assuming these.
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
- Do not create SystemC or C++ functional models — that is a separate,
  opt-in skill (`systemc-model-generation`) for an IP whose template names
  `systemc` in `ip.modeling_backends`. This skill's own output is unaffected
  either way.
- Do not create per-IP folders, `perf_model.py`, or `functional_model.py`.
- Do not leave scaffold TODO comments in finalized models.

For detailed implementation and test rules, read
`references/model_generation_rules.md` and
`references/coding_style.md`. Before writing any FSM process that races a
resource request (`Store.get()`, `Resource.request()`, ...) against a
timeout — `yield request_event | timeout_event` inside a loop — read
`references/simpy_concurrency_patterns.md` first: two real bugs this
project's own models shipped, verified against SimPy's actual source, both
from the same mistake (a fresh request every iteration) that reads as
perfectly reasonable code.
