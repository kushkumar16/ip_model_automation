# Model Generation Notes

The finalized DLDs are human-readable intent. The IP templates are the source
of truth for model generation.

Current generated model target:

- SimPy transaction-level delay models.

Generated artifacts:

- `templates/*.template.yaml`: normalized templates with FSM relationships,
  timing, functionality, and test intent.
- `src/ip_model_automation/<ip_name>.py`: one flat SimPy delay model file per
  IP block.
- `src/ip_model_automation/common.py`: shared dataclasses, registry metadata,
  and helpers.
- `src/ip_model_automation/ip.py`: public export surface for all IP models.
- `tests/test_ip_simpy_models.py`: registry, layout, and basic SimPy execution
  tests.

The repo intentionally avoids per-IP Python model folders and files named
`perf_model.py`, `functional_model.py`, or `soc_models.py`.

Run Python tests from the repo root:

```powershell
$env:PYTHONPATH="$PWD\src"
python -m unittest discover -s tests
```
