# SimPy IP Model Generation Skill

Use this skill after an IP DLD has been converted into a reviewed
`*.template.yaml` file.

The goal is to generate SimPy transaction-level delay models from the template.
The template is the source of truth for functionality, FSM processes, timing,
queues, resources, metrics, and tests.

## Source Priority

1. Treat the IP template as the source of truth for code generation.
2. Use the DLD only while authoring or reviewing the template.
3. During model generation, do not reread the DLD to infer missing behavior.
4. If the template is incomplete, stop generation and report the exact missing
   template fields.
5. Do not invent protocols, FSMs, queues, resources, timing values, or side
   effects not present in the template.

## Flat Python Layout

Do not create per-IP model folders.

Keep one flat Python file per IP under:

```text
src/ip_model_automation/
```

Use this naming convention:

```text
src/ip_model_automation/<ip_name>.py
```

Keep shared dataclasses, the IP registry, and small common helpers in:

```text
src/ip_model_automation/common.py
```

Keep `ip.py` as the public export surface and registry-facing module. It should
not contain IP model implementations.

Do not generate:

- `systemc/`
- `perf_model.py`
- `functional_model.py`
- `soc_models.py`
- per-IP Python folders

## SimPy Model Rules

For each IP file:

- Generate one class named `<CamelIpName>Model`.
- Generate one SimPy process method per `fsm_processes` entry where
  `simpy_process: true`.
- Use `timing_model.fsm_process_delays` as the only source for hard-coded
  delays.
- Implement command validity, error, and completion behavior from `commands`.
- Implement queues and resources from the template.
- Preserve sequential paths from `fsm_relationships.sequential_paths`.
- Start parallel FSMs independently using `env.process(...)`.
- Track metrics from `performance_model.metrics`.
- Track FSM/state coverage for unit tests.
- Use `get_ip_logger("<ip_name>", log_level, log_file)` for logging.
- Each model constructor must accept `log_level: str = "WARNING"` and
  `log_file: str = "run.log"`.
- Emit IP-tagged logs to terminal and `run.log`: `DEBUG` for FSM detail,
  `INFO` for command lifecycle, `WARNING` for stalls/backpressure, `ERROR` for
  invalid/error paths, and `CRITICAL` for fatal/safety events.

## Scaffold Generator

Before hand-writing a new model, generate a scaffold from the reviewed
template:

```powershell
python tools/generate_model_scaffold.py templates/<ip_name>.template.yaml --output-dir src/ip_model_automation
```

The scaffold is not the final model. It creates the flat file, class name,
common state containers, resources, queues, metrics, and one SimPy process per
FSM. After scaffold generation, fill the TODO hooks using only template fields:

- `commands` for validity, completion, and error behavior.
- `fsm_processes` for states, transitions, actions, interfaces, queues, and
  resources.
- `fsm_relationships` for sequential and parallel behavior.
- `timing_model.fsm_process_delays` for all hard-coded delays.
- `functionality_model.state_variables` and `performance_model.metrics` for
  implementation state and observations.
- `test_scenarios` for unit-test coverage.

Do not keep scaffold TODO comments in finalized IP models.

## Unit Test Rules

Use Python `unittest`.

Tests may import concrete models from either:

```python
from ip_model_automation.ip import <CamelIpName>Model
```

or:

```python
from ip_model_automation.<ip_name> import <CamelIpName>Model
```

Tests must validate:

- registry contains every IP
- DLD/template paths exist
- each IP has an individual flat Python file
- each IP model can execute a basic SimPy scenario
- expected functionality and delay properties from `test_scenarios`

Run:

```powershell
$env:PYTHONPATH="$PWD\src"
python -m unittest discover -s tests
```

## Required Outputs

- `src/ip_model_automation/<ip_name>.py`
- `src/ip_model_automation/common.py`
- `src/ip_model_automation/ip.py`
- `tests/test_ip_simpy_models.py`
- `docs/model_generation_notes.md`

## Validation Checklist

- Run `tools/template_lint.py` on all templates.
- Run `tools/generate_model_scaffold.py` for new templates before model
  implementation.
- Run unit tests.
- Confirm no `systemc/` directory remains.
- Confirm no per-IP Python model folders exist.
- Confirm no `perf_model.py`, `functional_model.py`, or `soc_models.py` files
  remain.
