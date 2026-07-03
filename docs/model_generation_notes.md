# Model Generation Notes

The finalized DLDs are human-readable intent. The IP templates are the source
of truth for model generation.

The DLD front-end (`tools/dld_to_template.py`) extracts a *draft* template and a
gaps report from a DLD. DLDs vary in format and omit details, so the extractor
marks anything it cannot derive with `TODO_REVIEW` and lists it in the gaps
report rather than inventing behavior. A reviewer/LLM completes the draft, checks
DLD coverage (`tools/check_template_coverage.py`), and promotes it to the golden
`templates/<ip>.template.yaml`.

Current generated model target:

- SimPy transaction-level delay models.

Generated artifacts:

- `templates/*.template.draft.yaml`: extractor output awaiting review (gitignored).
- `reports/*.gaps.md`: per-IP missing-detail / open-items report (gitignored).
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
