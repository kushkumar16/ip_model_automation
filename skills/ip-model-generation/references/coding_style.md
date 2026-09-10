# Coding Style Guide

Every Python file in `src/`, `tools/`, and `tests/` follows this guide. The
mechanical half is enforced by ruff (configuration in `ruff.toml` at the repo
root); the structural half is enforced by the scaffold/validation gates and by
review. Check locally with:

```powershell
python tools\check_code_style.py          # check (the pipeline gate)
python tools\check_code_style.py --fix    # auto-fix lint findings and reformat
```

## Formatting (enforced by `ruff format`)

- Line length: 120 columns. Break long argument lists one-per-line rather than
  disabling the limit; long human-readable strings may be split with implicit
  concatenation.
- Double quotes for strings; four-space indentation; trailing commas in
  multi-line literals (the formatter applies all of this — do not hand-format
  against it).

## Lint rules (enforced by `ruff check`)

- pycodestyle (`E`, `W`) and pyflakes (`F`): no unused imports or variables,
  no ambiguous single-letter names like `l`.
- Import sorting (`I`): three groups separated by blank lines — stdlib,
  third-party (`simpy`, `yaml`), then first-party (`ip_model_automation`),
  each alphabetized. Models inside the package use relative imports
  (`from .common import ...`).

## File and module conventions

- `from __future__ import annotations` first in every module.
- One **flat** model file per IP: `src/ip_model_automation/<ip_name>.py`.
  Never per-IP folders, and never files named `perf_model.py`,
  `functional_model.py`, or `soc_models.py`.
- Shared dataclasses and helpers live in `common.py`; `ip.py` is the public
  export/registry surface. Tools get a one-line docstring header describing
  what they do.

## Model class conventions

- Class name is the CamelCase of the IP name plus `Model`
  (`completion_ip` -> `CompletionIpModel`), with a short docstring stating what the
  model represents and where its timing comes from.
- Constructor signature order: `env` first; per-operation latency parameters
  (named `<operation>_latency`, defaulted from the template's timing model);
  behavioral knobs; then `log_level: str = "WARNING"` and
  `log_file: str = "run.log"` last.
- Initialize `self.logger` via `get_ip_logger("<ip_name>", log_level, log_file)`;
  log with `%s` lazy formatting, never f-strings, at the levels the generation
  rules define (DEBUG transitions, INFO lifecycle, WARNING expected stalls,
  ERROR invalid paths).
- State the tests observe: a `self.metrics` defaultdict of counters
  (snake_case names such as `token_stalls`, `completed_commands`) and, where
  useful, a `self.fsm_state` dict keyed by template FSM name.
- One SimPy process method per template FSM, named `<fsm_name>_process` (or
  the FSM name itself for scheduler-style loops), started in the constructor
  with `env.process(...)`.
- Private helpers take a leading underscore; public API methods match
  `functionality_model.apis` in the template.
- Type hints on public method signatures; payloads stay abstract.

## Test conventions

- `unittest`, one `tests/test_<ip_name>.py` per IP, class
  `Test<ClassName>`, methods named `test_<ip>_<behavior>` after the template's
  `test_scenarios`.
- Construct models with small explicit latency overrides so tests run in a few
  simulated ticks; assert on `metrics`, completion records, and token/queue
  state — not on log output.
