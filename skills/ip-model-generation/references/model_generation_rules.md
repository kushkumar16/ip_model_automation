# Model Generation Rules

## Source Priority

1. Treat the template as the source of truth for generation.
2. Use the DLD only while authoring or reviewing the template.
3. During model implementation, stop if required behavior is missing from the
   template.
4. Do not invent FSMs, queues, resources, timing, side effects, or tests.

## Required Model Shape

- Put the model in `src/ip_model_automation/<ip_name>.py`.
- Keep `ip.py` as the public export and registry surface.
- Keep shared helpers in `common.py`.
- Use SimPy stores/resources/events for process communication.
- Track `metrics` and, where useful, `fsm_state`.
- Expose simple APIs listed in `functionality_model.apis`.
- Import and use `get_ip_logger` from `common.py`.
- Each model constructor must accept `log_level: str = "WARNING"` and
  `log_file: str = "run.log"`, initialize `self.logger`, and write logs to both
  terminal and the log file with the IP tag, for example `[arbitration_ip]`.
- Follow the coding style guide (`references/coding_style.md`): 120-column
  ruff formatting, sorted imports, and the model/test naming conventions. The
  pipeline enforces it with `python tools/check_code_style.py`.

## FSM Implementation

- Start every parallel FSM with `env.process(...)`.
- Preserve sequential paths by handing transactions through queues/stores.
- Model backpressure when interfaces, queues, resources, or config gates block progress.
- Use template timing operations for timeouts; tests may override constructor delays for speed.
- Keep payloads abstract unless the template explicitly requires payload modeling.
- Log FSM transitions at `DEBUG`, command lifecycle at `INFO`, expected stalls
  and backpressure at `WARNING`, invalid/error paths at `ERROR`, and terminal
  safety/fatal conditions at `CRITICAL`.

## Unit Tests

- Use `unittest`.
- Put tests in `tests/test_<ip_name>.py`.
- Cover all scenarios in `test_scenarios`.
- Assert both functionality and performance/modeling effects:
  - completions/responses/interrupts
  - queue hold or backpressure behavior
  - token/credit/burst debit behavior
  - expected stall counters
  - FSM coverage intent from the template

## Amending An Existing Model

If the model already exists and its DLD changed, do not regenerate it. Work from
the structured template diff (`tools/diff_template.py`, or the amend prompt the
pipeline pipes you): make only the edits the delta requires, leave unrelated code
untouched, and treat a `STRUCTURAL` change (FSM/state/interface/command added or
removed) as a signal to check for cascade before editing. See the "Stage 2"
section of the agent contract.

## Validation

Run:

```powershell
python tools\template_lint.py templates\*.template.yaml
python tools\report_model_coverage.py
python tools\check_code_style.py
python tools\validate_ip_flow.py
```

The final validation must pass before considering the model complete.
