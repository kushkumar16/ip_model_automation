# systemc/

The SystemC backend's actual artifacts. For the workflow, idioms, and rules
that govern what goes in this directory, see the
[`systemc-model-generation`](../skills/systemc-model-generation/SKILL.md)
skill — this file is a quick directory reference, not the authoritative
guide.

## Layout

```text
systemc/
  models/<ip>.h        # SC_MODULE declaration
  models/<ip>.cpp       # process implementations
  tests/test_<ip>.cpp   # compiled testbench, one binary per IP
```

One flat header/source pair per IP, mirroring
`src/ip_model_automation/<ip>.py`'s own "one flat file per IP" convention —
not a per-IP folder.

## Quick commands

```shell
# opt an IP in: add `modeling_backends: [simpy, systemc]` to its template's ip: block
python tools/generate_systemc_scaffold.py templates/<ip>.template.yaml
# implement the model, then:
python tools/run_systemc_tests.py <ip>
```

Requires the SystemC development library (`apt-get install libsystemc-dev`;
`pkg-config systemc` reports whether it is installed). `tools/run_ci.py`'s
`systemc_tests` gate runs this for every opted-in IP and is a no-op
otherwise.

## Registry

There is no `IP_ARTIFACTS`-style registry for SystemC the way `common.py`
holds one for SimPy — `tools/run_systemc_tests.py`'s `systemc_ips()`
discovers opted-in IPs by reading every template's `modeling_backends`
directly, on every run, so it can never drift from what the templates
actually declare.
