import contextlib
import copy
import importlib.util
import io
import logging
import re
import sys
import tempfile
import unittest
from pathlib import Path

import simpy
import yaml

from ip_model_automation.common import get_ip_logger
from ip_model_automation.ip import IP_ARTIFACTS, ArbitrationIpModel, Command, list_ips, resolve_artifacts


class TestIpRegistryAndLayout(unittest.TestCase):
    def test_registry_has_all_ips_and_artifacts(self):
        self.assertEqual(len(tuple(list_ips())), 2)
        repo_root = Path(__file__).resolve().parents[1]
        for ip_name in IP_ARTIFACTS:
            paths = resolve_artifacts(repo_root, ip_name)
            self.assertTrue(paths["dld"].is_file())
            self.assertTrue(paths["template"].is_file())

    def test_each_ip_has_flat_python_file(self):
        repo_root = Path(__file__).resolve().parents[1]
        package_dir = repo_root / "src" / "ip_model_automation"
        expected = {
            "__init__.py",
            "arbitration_ip.py",
            "common.py",
            "completion_ip.py",
            "ip.py",
        }
        self.assertEqual({path.name for path in package_dir.glob("*.py")}, expected)
        self.assertFalse(any(path.is_dir() and path.name.endswith("_ip") for path in package_dir.iterdir()))

    def test_generator_scaffold_emits_flat_model_file(self):
        repo_root = Path(__file__).resolve().parents[1]
        generator_path = repo_root / "tools" / "generate_model_scaffold.py"
        spec = importlib.util.spec_from_file_location("generate_model_scaffold", generator_path)
        generator = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(generator)

        with tempfile.TemporaryDirectory() as tmpdir:
            target = generator.write_scaffold(
                repo_root / "templates" / "completion_ip.template.yaml",
                Path(tmpdir),
                stdout=False,
            )
            self.assertTrue(target.is_file())
            text = target.read_text(encoding="utf-8")
            self.assertIn("class CompletionIpModel", text)
            self.assertIn("def accept_process", text)
            self.assertIn("def completion_scheduler_process", text)
            self.assertIn("def refill_process", text)

    def test_validation_flow_scaffold_check_all_templates(self):
        repo_root = Path(__file__).resolve().parents[1]
        validator_path = repo_root / "tools" / "validate_ip_flow.py"
        spec = importlib.util.spec_from_file_location("validate_ip_flow", validator_path)
        validator = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(validator)

        templates = validator.discover_templates(repo_root)
        self.assertEqual(len(templates), 2)
        validator.validate_scaffolds(repo_root, templates)

    def test_prompt_pack_generator_emits_model_request(self):
        repo_root = Path(__file__).resolve().parents[1]
        generator_path = repo_root / "tools" / "generate_prompt_pack.py"
        spec = importlib.util.spec_from_file_location("generate_prompt_pack", generator_path)
        generator = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(generator)

        with tempfile.TemporaryDirectory() as tmpdir:
            target = generator.write_prompt_pack(
                repo_root / "templates" / "arbitration_ip.template.yaml",
                Path(tmpdir),
                stdout=False,
            )
            self.assertTrue(target.is_file())
            text = target.read_text(encoding="utf-8")
            self.assertIn("# Prompt Pack: arbitration_ip", text)
            self.assertIn("Model class: `ArbitrationIpModel`", text)
            self.assertIn("## Required Unit Tests From Template", text)

    def test_harness_config_is_inspectable(self):
        repo_root = Path(__file__).resolve().parents[1]
        inspector_path = repo_root / "tools" / "inspect_harness.py"
        spec = importlib.util.spec_from_file_location("inspect_harness", inspector_path)
        inspector = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(inspector)

        lines = inspector.inspect(repo_root, repo_root / "harness" / "ip_generation_loop.yaml")
        text = "\n".join(lines)
        self.assertIn("harness: ip_generation_loop", text)
        self.assertIn("agent_contract: agents/ip_model_generation_agent.md", text)
        self.assertIn("templates: 2", text)

        # Every contract an agent stage is told to follow must be a file that
        # exists. A stage pointing at a missing contract is an agent invoked with
        # no instructions, which fails as vague output rather than as an error.
        harness = yaml.safe_load((repo_root / "harness" / "ip_generation_loop.yaml").read_text(encoding="utf-8"))
        contracts = [v for k, v in harness.items() if k.endswith("_contract")]
        self.assertEqual(len(contracts), 4, "expected one contract per agent-driven stage family")
        for contract in contracts:
            self.assertTrue((repo_root / contract).is_file(), f"missing agent contract: {contract}")

    def test_agent_profile_resolution_supports_any_vendor(self):
        repo_root = Path(__file__).resolve().parents[1]
        pipeline_path = repo_root / "tools" / "auto_ip_pipeline.py"
        spec = importlib.util.spec_from_file_location("auto_ip_pipeline", pipeline_path)
        pipeline = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(pipeline)

        harness = pipeline.load_harness()
        profiles = harness.get("agent_profiles") or {}
        self.assertIn("claude", profiles)
        self.assertIn("codex", profiles)
        self.assertTrue(all(isinstance(cmd, str) and cmd.strip() for cmd in profiles.values()))

        resolve = pipeline.resolve_agent_command
        self.assertIsNone(resolve(None, None, harness))
        self.assertEqual(resolve("codex", None, harness), profiles["codex"])
        self.assertEqual(resolve("codex", "my-agent --auto", harness), "my-agent --auto")
        with self.assertRaises(SystemExit):
            resolve("unknown-agent", None, harness)

    def test_reference_template_is_lint_clean_and_undiscovered(self):
        repo_root = Path(__file__).resolve().parents[1]
        reference = repo_root / "templates" / "reference_template.yaml"
        self.assertTrue(reference.is_file())

        linter_path = repo_root / "tools" / "template_lint.py"
        spec = importlib.util.spec_from_file_location("template_lint", linter_path)
        linter = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(linter)
        self.assertEqual(linter.lint_file(reference), [])

        validator_path = repo_root / "tools" / "validate_ip_flow.py"
        spec = importlib.util.spec_from_file_location("validate_ip_flow", validator_path)
        validator = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(validator)
        discovered = {path.name for path in validator.discover_templates(repo_root)}
        self.assertNotIn("reference_template.yaml", discovered)

    def test_template_lint_enforces_schema_and_semantics(self):
        repo_root = Path(__file__).resolve().parents[1]
        linter_path = repo_root / "tools" / "template_lint.py"
        spec = importlib.util.spec_from_file_location("template_lint", linter_path)
        linter = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(linter)

        # Every promoted template passes both layers (structure + semantics).
        for template in (repo_root / "templates").glob("*.template.yaml"):
            self.assertEqual(linter.lint_file(template), [], f"{template.name} is not lint-clean")

        import yaml

        base = yaml.safe_load((repo_root / "templates" / "completion_ip.template.yaml").read_text(encoding="utf-8"))

        # Layer 1 (schema) catches a structural violation: an unbounded queue
        # with no depth_note.
        broken_structure = copy.deepcopy(base)
        broken_structure["queues"][0]["depth"] = "unbounded"
        broken_structure["queues"][0].pop("depth_note", None)
        errors = linter.lint_template(broken_structure, importlib.import_module("jsonschema"))
        self.assertTrue(any("schema[" in e and "depth_note" in e for e in errors), errors)

        # Layer 2 (semantics) catches a cross-field violation JSON Schema cannot
        # express: a declared fsm_count that disagrees with fsm_processes.
        broken_semantics = copy.deepcopy(base)
        broken_semantics["fsm_relationships"]["fsm_count"] = 99
        errors = linter.lint_template(broken_semantics, importlib.import_module("jsonschema"))
        self.assertTrue(any("fsm_count=99" in e for e in errors), errors)

    def test_interface_wait_models_are_stated_and_placed(self):
        """Every interface declares one of the three wait models, tied to a real FSM state."""
        repo_root = Path(__file__).resolve().parents[1]
        extractor_path = repo_root / "tools" / "dld_to_template.py"
        spec = importlib.util.spec_from_file_location("dld_to_template", extractor_path)
        extractor = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(extractor)

        import yaml

        for template_path in (repo_root / "templates").glob("*.template.yaml"):
            template = yaml.safe_load(template_path.read_text(encoding="utf-8"))
            states = {
                fsm["name"]: {s["name"] if isinstance(s, dict) else s for s in fsm["states"]}
                for fsm in template["fsm_processes"]
            }
            for interface in template["interfaces"]:
                wait_model = interface.get("wait_model")
                self.assertIsNotNone(wait_model, f"{template_path.name}: {interface['name']} has no wait_model")
                self.assertIn(wait_model["mode"], extractor.WAIT_MODES)
                # A promoted template must not be carrying the assumed fallback.
                self.assertEqual(
                    wait_model["source"],
                    "dld",
                    f"{template_path.name}: {interface['name']} wait model is not stated in the DLD",
                )
                for point in wait_model["wait_points"]:
                    fsm, state = point.split(".")
                    self.assertIn(state, states.get(fsm, set()), f"{template_path.name}: bad wait point {point}")

        # A DLD interface section without a `Wait model:` block falls back to
        # approach 2, and says so rather than passing the assumption off as stated.
        silent = ["Fields:", "", "- `addr`", ""]
        assumed = extractor.extract_wait_model(silent, "input")
        self.assertEqual(assumed["mode"], "wait_for_ack_inline")
        self.assertEqual(assumed["source"], "assumed_default")
        self.assertEqual(assumed["requester"], "peer")
        self.assertEqual(assumed["wait_points"], [extractor.TODO])

        stated = extractor.extract_wait_model(
            [
                "Wait model:",
                "",
                "- Mode: `wait_for_ack_before_next_request`",
                "- Requester: this IP",
                "- Waits in: `example_fsm.WAIT_CLEAR`",
                "- Resumes on: `software_clear`",
                "- Outstanding limit: 2",
                "",
            ],
            "output",
        )
        self.assertEqual(
            stated,
            {
                "mode": "wait_for_ack_before_next_request",
                "requester": "this_ip",
                "wait_points": ["example_fsm.WAIT_CLEAR"],
                "resumes_on": "software_clear",
                "source": "dld",
                "outstanding_limit": 2,
            },
        )

    def test_models_enter_every_declared_wait_point(self):
        """The other half of the wait-model contract: the model must implement it."""
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "check_wait_model_coverage.py"
        spec = importlib.util.spec_from_file_location("check_wait_model_coverage", tool_path)
        tool = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(tool)

        self.assertEqual(tool.check_all(), [], "a model does not enter a wait point its template declares")

        # A model that asserts and moves on, where the template says it waits
        # for the software clear, is exactly what this gate exists to catch.
        source = 'self.fsm_state["interrupt_notify"] = "ASSERT_IRQ"\n'
        self.assertTrue(tool.enters_state(source, "interrupt_notify", "ASSERT_IRQ"))
        self.assertFalse(tool.enters_state(source, "interrupt_notify", "WAIT_SW_CLEAR"))
        self.assertTrue(tool.enters_state('self._set_fsm_state("arbiter_main", "SQ_SCAN")', "arbiter_main", "SQ_SCAN"))

    def test_wait_model_violations_fail_the_gates(self):
        repo_root = Path(__file__).resolve().parents[1]
        linter_path = repo_root / "tools" / "template_lint.py"
        spec = importlib.util.spec_from_file_location("template_lint", linter_path)
        linter = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(linter)

        import yaml

        jsonschema = importlib.import_module("jsonschema")
        base = yaml.safe_load(
            # A fixture template, not a live IP: this test mutates specific
            # queues and FSMs by name, so it needs a document whose shape is
            # fixed independently of which IPs the repo currently ships.
            (Path(__file__).resolve().parent / "fixtures" / "templates" / "mailbox_ip.template.yaml").read_text(
                encoding="utf-8"
            )
        )

        # Layer 1 (schema): a missing wait model, a mode-3 interface with no
        # outstanding limit, and an assumed default that is not approach 2.
        missing = copy.deepcopy(base)
        missing["interfaces"][0].pop("wait_model")
        self.assertTrue(
            any("wait_model" in e and "required" in e for e in linter.lint_template(missing, jsonschema)),
        )

        unbounded = copy.deepcopy(base)
        deferred = next(i for i in unbounded["interfaces"] if i["wait_model"]["mode"].endswith("next_request"))
        deferred["wait_model"].pop("outstanding_limit")
        self.assertTrue(
            any("outstanding_limit" in e for e in linter.lint_template(unbounded, jsonschema)),
        )

        mislabelled = copy.deepcopy(base)
        mislabelled["interfaces"][0]["wait_model"]["source"] = "assumed_default"
        self.assertTrue(
            any("wait_for_ack_inline" in e for e in linter.lint_template(mislabelled, jsonschema)),
        )

        # Layer 2 (semantics): a wait point that names a state the FSM does not have.
        misplaced = copy.deepcopy(base)
        misplaced["interfaces"][0]["wait_model"]["wait_points"] = ["message_push.NOT_A_STATE"]
        self.assertTrue(
            any("unknown state" in e for e in linter.lint_template(misplaced, jsonschema)),
        )

    def test_target_profile_defaults_match_repo_and_override(self):
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "target_profile.py"
        spec = importlib.util.spec_from_file_location("target_profile", tool_path)
        tp = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(tp)

        # Default profile resolves to this repo's layout, and its model_class
        # convention matches the actual class in every promoted model file.
        profile = tp.load_profile(repo_root)
        self.assertEqual(profile.model_dir, repo_root / "src" / "ip_model_automation")
        for template in profile.templates_dir.glob("*.template.yaml"):
            ip = template.name[: -len(".template.yaml")]
            model_file = profile.model_file(ip)
            self.assertTrue(model_file.is_file(), f"{ip}: model file not found via profile")
            self.assertIn(
                f"class {profile.model_class(ip)}",
                model_file.read_text(encoding="utf-8"),
                f"{ip}: profile.model_class does not match the actual model class",
            )

        # A foreign profile redirects every path and renames artifacts, with no
        # tool-code change — the seam that enables reuse in another framework.
        foreign = tp.Profile(
            Path("/other/framework"),
            {
                "model_dir": "sim/models",
                "tests_dir": "sim/tests",
                "templates_dir": "specs",
                "dlds_dir": "docs",
                "reports_dir": "out",
                "prompt_packs_dir": "out/prompts",
            },
            {
                "model_file": "{ip}_model.py",
                "test_file": "{ip}_test.py",
                "template_file": "{ip}.yaml",
                "draft_file": "{ip}.draft.yaml",
                "dld_file": "{ip}.md",
                "model_class": "Sim{camel}",
            },
        )
        self.assertEqual(foreign.model_file("mailbox_ip").as_posix(), "/other/framework/sim/models/mailbox_ip_model.py")
        self.assertEqual(foreign.model_class("mailbox_ip"), "SimMailboxIp")

    def test_pipeline_selects_generate_vs_amend_path(self):
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "auto_ip_pipeline.py"
        spec = importlib.util.spec_from_file_location("auto_ip_pipeline", tool_path)
        pipeline = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        sys.modules[spec.name] = pipeline
        spec.loader.exec_module(pipeline)

        # Greenfield vs brownfield is decided by the model_preexisted snapshot.
        new_ctx = {"model_preexisted": ""}
        old_ctx = {"model_preexisted": "1"}
        self.assertTrue(pipeline.STAGE_CONDITIONS["model_new"](new_ctx))
        self.assertFalse(pipeline.STAGE_CONDITIONS["model_exists"](new_ctx))
        self.assertFalse(pipeline.STAGE_CONDITIONS["model_new"](old_ctx))
        self.assertTrue(pipeline.STAGE_CONDITIONS["model_exists"](old_ctx))

        # The amend stage is wired: registered builder + a harness stage gated on model_exists.
        self.assertIn("amend_implementation", pipeline.AGENT_PROMPT_BUILDERS)
        harness = pipeline.load_harness()
        stages = {s["name"]: s for s in harness["stages"]}
        self.assertEqual(stages["agent_implementation"]["when"], "model_new")
        self.assertEqual(stages["amend_implementation"]["when"], "model_exists")
        self.assertIn("unit_tests", stages["amend_implementation"]["gates"])
        self.assertIn("stamp_provenance", stages)

        # amend_prompt exercises `git show HEAD:...`; with the working tree in sync
        # it finds no delta and falls back to the full implementation prompt.
        prompt = pipeline.amend_prompt("completion_ip")
        self.assertIn("completion_ip", prompt)

    def test_review_decisions_have_a_durable_tracked_home(self):
        """Decisions must not live in a file the tooling regenerates.

        They did: the gaps report's own instructions said "note it here", while
        `dld_to_template.py` rewrites that file on every parse and `reports/` is
        gitignored. A reviewer's reasoning — why a model behaves as it does —
        was erased by the next extraction and never reached the repository.
        """
        repo_root = Path(__file__).resolve().parents[1]
        decisions = repo_root / "decisions"
        self.assertTrue((decisions / "README.md").is_file(), "decisions/ must explain itself")

        ignored = (repo_root / ".gitignore").read_text(encoding="utf-8")
        self.assertNotIn("decisions/", ignored, "the durable home must not be gitignored")

        # No tool may write into it — that is the whole point of the directory.
        for tool in (repo_root / "tools").glob("*.py"):
            text = tool.read_text(encoding="utf-8")
            self.assertNotIn("decisions_dir", text, f"{tool.name} appears to generate into decisions/")

        # The instruction that caused the loss must be gone from every place that
        # carried it. It was fixed in two and left in two others for long enough
        # that the skill file -- the one the pipeline prompt tells the agent to
        # follow -- was still pointing at the regenerated report.
        carriers = {
            "agents/ip_model_generation_agent.md": "decisions/<ip_name>.md",
            "skills/ip-model-generation/SKILL.md": "decisions",
            "skills/ip-model-generation/references/dld_extraction_rules.md": "decisions/<ip>.md",
        }
        # The wrong destination was written across a line break in one of them, so
        # the text is compared with its whitespace flattened.
        forbidden = ("record it in the gaps report", "record it in the\ngaps report", "note it here")
        for relative, expected in carriers.items():
            text = (repo_root / relative).read_text(encoding="utf-8")
            flat = " ".join(text.split())
            self.assertIn(expected, text, f"{relative} must name the durable home")
            for phrase in forbidden:
                self.assertNotIn(
                    " ".join(phrase.split()),
                    flat.lower(),
                    f"{relative} still sends the record to a regenerated file",
                )

        spec = importlib.util.spec_from_file_location("auto_ip_pipeline", repo_root / "tools" / "auto_ip_pipeline.py")
        pipeline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pipeline
        spec.loader.exec_module(pipeline)
        self.assertIn("decisions/completion_ip.md", pipeline.complete_template_prompt("completion_ip"))

        # The extractor prints the same instruction into every gaps report it
        # generates, so it is a carrier too -- and the one that reaches a reader
        # who never opens the skill.
        extractor_source = (repo_root / "tools" / "dld_to_template.py").read_text(encoding="utf-8")
        self.assertIn("`decisions/{ip_name}.md`", extractor_source)
        self.assertIn("Do not record it", extractor_source)

    def test_generated_reports_never_destroy_hand_written_content(self):
        """Regenerating a report must preserve anything a person wrote in it.

        The durable home above is the fix; this is the safety net for content
        still written in the old place, and for the next generated file someone
        decides to annotate.
        """
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("dld_to_template", repo_root / "tools" / "dld_to_template.py")
        extractor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(extractor)

        # A stamped generation recognises itself, so the warning does not cry wolf.
        stamped = extractor.stamp_generated("# Gaps Report\n\nsome generated body\n")
        self.assertTrue(extractor.is_untouched_generation(stamped))

        # One added line is enough to make it no longer the tool's own output.
        self.assertFalse(extractor.is_untouched_generation(stamped + "\n**Resolved:** a human wrote this.\n"))
        self.assertFalse(extractor.is_untouched_generation("no marker at all"))

        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "probe_ip.gaps.md"
            report.write_text(stamped + "\n**Resolved:** a human wrote this.\n", encoding="utf-8")
            kept = extractor.preserve_hand_edits(report, "probe_ip")
            self.assertIsNotNone(kept)
            self.assertIn("a human wrote this", kept.read_text(encoding="utf-8"))

            # An untouched generation is left alone — no clutter, no warning.
            report.write_text(stamped, encoding="utf-8")
            self.assertIsNone(extractor.preserve_hand_edits(report, "probe_ip"))

    def test_review_findings_gate_accounts_for_every_finding(self):
        """The deterministic half of the reviewer stage, built before the reviewer.

        A reviewer's verdict is sampled — ask twice, get two answers — so it never
        votes. It writes an artifact and this gate asks a mechanical question:
        is every recorded finding fixed or explicitly dismissed?
        """
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "check_review_findings", repo_root / "tools" / "check_review_findings.py"
        )
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)

        import yaml  # noqa: PLC0415

        src, normalized = "author source\n", "normalized document\n"
        base = {
            "ip": "probe_ip",
            "kind": "normalization",
            "source_sha256": gate.sha256_text(src),
            "normalized_sha256": gate.sha256_text(normalized),
            "findings": [
                {
                    "id": "F1",
                    "class": "timing_attachment",
                    "severity": "high",
                    "claim": "A stated delay is on the wrong FSM.",
                    "source": "6.0 Timing",
                    "normalized": "6. FSM Timing Model",
                    "why": "The model would charge it to the wrong process.",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dlds").mkdir()
            (root / "reviews").mkdir()
            (root / "decisions").mkdir()
            (root / "dlds" / "probe_ip_dld.src.md").write_text(src, encoding="utf-8")
            (root / "dlds" / "probe_ip_dld.md").write_text(normalized, encoding="utf-8")
            gate.REPO_ROOT, gate.DLDS_DIR, gate.REVIEWS_DIR, gate.DECISIONS_DIR = (
                root,
                root / "dlds",
                root / "reviews",
                root / "decisions",
            )
            findings_file = root / "reviews" / "probe_ip.normalization.findings.yaml"

            def write(data):
                findings_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

            # An unresolved finding blocks.
            write(base)
            errors = gate.check_ip("probe_ip", "normalization")
            self.assertTrue(any("F1" in e and "unresolved" in e for e in errors), errors)

            # Dismissal by name, with a reason, in the tracked decisions file.
            (root / "decisions" / "probe_ip.md").write_text(
                "- **F1 dismissed:** the table row is on the scrub FSM; the finding misreads it.\n",
                encoding="utf-8",
            )
            self.assertEqual(gate.check_ip("probe_ip", "normalization"), [])

            # Editing either document makes the review stale: an old clean review
            # must not vouch for new text.
            (root / "dlds" / "probe_ip_dld.md").write_text(normalized + "edit\n", encoding="utf-8")
            self.assertTrue(any("stale" in e for e in gate.check_ip("probe_ip", "normalization")))
            (root / "dlds" / "probe_ip_dld.md").write_text(normalized, encoding="utf-8")

            # A malformed file is an unknown result, never an absence of findings.
            findings_file.write_text("findings: [oops\n", encoding="utf-8")
            self.assertTrue(any("valid YAML" in e for e in gate.check_ip("probe_ip", "normalization")))

            broken = copy.deepcopy(base)
            broken["findings"][0]["class"] = "vibes"
            write(broken)
            self.assertTrue(any("unknown class" in e for e in gate.check_ip("probe_ip", "normalization")))

            missing_field = copy.deepcopy(base)
            del missing_field["findings"][0]["why"]
            write(missing_field)
            self.assertTrue(any("missing `why`" in e for e in gate.check_ip("probe_ip", "normalization")))

            duplicated = copy.deepcopy(base)
            duplicated["findings"].append(copy.deepcopy(base["findings"][0]))
            write(duplicated)
            self.assertTrue(any("duplicate" in e for e in gate.check_ip("probe_ip", "normalization")))

            # No findings file at all: nothing recorded, nothing to account for.
            findings_file.unlink()
            self.assertEqual(gate.check_ip("probe_ip", "normalization"), [])

            # A model review is a claim about a different set of files, with its
            # own vocabulary. A normalization class must not be usable here.
            (root / "templates").mkdir()
            (root / "src" / "ip_model_automation").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "templates" / "probe_ip.template.yaml").write_text("t\n", encoding="utf-8")
            (root / "src" / "ip_model_automation" / "probe_ip.py").write_text("m\n", encoding="utf-8")
            (root / "tests" / "test_probe_ip.py").write_text("x\n", encoding="utf-8")
            model_review = {
                "ip": "probe_ip",
                "kind": "model",
                "template_sha256": gate.sha256_text("t\n"),
                "model_sha256": gate.sha256_text("m\n"),
                "tests_sha256": gate.sha256_text("x\n"),
                "findings": [
                    {
                        "id": "M1",
                        "class": "timing_mismatch",
                        "severity": "high",
                        "claim": "A delay differs from the template.",
                        "template": "timing_model...cycles: 6",
                        "model": "probe_ip.py:48 uses 12",
                        "why": "Simulated timing would be wrong.",
                    }
                ],
            }
            model_file = root / "reviews" / "probe_ip.model.findings.yaml"
            model_file.write_text(yaml.safe_dump(model_review, sort_keys=False), encoding="utf-8")
            self.assertTrue(any("M1" in e and "unresolved" in e for e in gate.check_ip("probe_ip", "model")))

            wrong_vocabulary = copy.deepcopy(model_review)
            wrong_vocabulary["findings"][0]["class"] = "timing_attachment"  # a normalization class
            model_file.write_text(yaml.safe_dump(wrong_vocabulary, sort_keys=False), encoding="utf-8")
            self.assertTrue(any("unknown class" in e for e in gate.check_ip("probe_ip", "model")))

            # Editing the model — not the DLD — makes a model review stale.
            model_file.write_text(yaml.safe_dump(model_review, sort_keys=False), encoding="utf-8")
            (root / "src" / "ip_model_automation" / "probe_ip.py").write_text("m2\n", encoding="utf-8")
            self.assertTrue(any("stale" in e for e in gate.check_ip("probe_ip", "model")))

    def test_finding_ids_are_unique_across_an_ips_reviews(self):
        """One dismissal must not silently clear two different findings.

        Dismissals live in a single decisions/<ip>.md and are matched by id, so
        `F1` meaning one thing in a normalization review and another in a model
        review would let one line resolve both.
        """
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "check_review_findings", repo_root / "tools" / "check_review_findings.py"
        )
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)

        for ip in gate.reviewed_ips():
            self.assertEqual(gate.duplicate_ids(ip), [], f"{ip} reuses a finding id across reviews")

    def test_review_findings_directory_is_tracked_and_documented(self):
        """Findings must survive until resolved, so they cannot be gitignored."""
        repo_root = Path(__file__).resolve().parents[1]
        self.assertTrue((repo_root / "reviews" / "README.md").is_file())

        ignored = (repo_root / ".gitignore").read_text(encoding="utf-8")
        self.assertNotIn("reviews/", ignored)

        readme = (repo_root / "reviews" / "README.md").read_text(encoding="utf-8")
        # The asymmetry is the whole safety argument; it must be stated where
        # someone writing a findings file will read it.
        self.assertIn("may never pass one", readme)

    def test_ci_runs_every_repo_wide_gate_the_harness_declares(self):
        """CI reads the harness, so a new repo-wide gate cannot escape it.

        Listing the gates a second time in the CI runner would let the two drift:
        someone adds a `scope: repo` stage, the pipeline runs it, and CI silently
        does not. The harness stays the single source of truth.
        """
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("run_ci", repo_root / "tools" / "run_ci.py")
        ci = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ci)

        import yaml  # noqa: PLC0415 — the harness is the fixture here

        harness = yaml.safe_load((repo_root / "harness" / "ip_generation_loop.yaml").read_text(encoding="utf-8"))
        declared = [s["name"] for s in harness["stages"] if s.get("scope") == "repo"]
        self.assertTrue(declared, "the harness declares no repo-wide stages")
        self.assertEqual([name for name, _cmd, _why in ci.harness_repo_gates()], declared)

        # The extra guards are real tools, not stale names.
        for name, command, why in ci.EXTRA_CHECKS:
            tool = command.split()[1]
            self.assertTrue((repo_root / tool).is_file(), f"{name} points at a missing tool: {tool}")
            self.assertTrue(why.strip(), f"{name} must say what it guards")

        # A failing gate must actually fail the run, not be reported and ignored.
        ok, _output, _seconds = ci.run_gate("probe", f'"{sys.executable}" -c "raise SystemExit(3)"')
        self.assertFalse(ok)

    def test_ci_checks_the_environment_it_cannot_assume(self):
        """Local CI has no clean room, so it verifies the one a runner would give.

        A hosted runner installs requirements.txt from scratch, so an undeclared
        dependency fails on the first run. Here a gate can quietly depend on a
        package someone installed by hand — python-docx was exactly that, needed
        by check_overview_sync and the docx DLD path and declared nowhere.
        """
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("run_ci", repo_root / "tools" / "run_ci.py")
        ci = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ci)

        self.assertEqual(ci.missing_requirements(), [], "this environment does not match requirements.txt")

        requirements = (repo_root / "requirements.txt").read_text(encoding="utf-8")
        for package in ("python-docx", "PyYAML", "jsonschema", "ruff", "coverage"):
            self.assertIn(package, requirements, f"{package} is used by a gate but not declared")

        # Every third-party import in tools/ must be declared, or local CI passes
        # on a machine where it happens to be installed and fails on a clone.
        declared = {line.split("=")[0].split(">")[0].split("<")[0].strip().lower() for line in requirements.split()}
        aliases = {"docx": "python-docx", "yaml": "pyyaml"}
        for tool in (repo_root / "tools").glob("*.py"):
            for match in re.finditer(r"^\s*(?:from|import) (\w+)", tool.read_text(encoding="utf-8"), re.M):
                module = match.group(1)
                if module in aliases:
                    self.assertIn(aliases[module], declared, f"{tool.name} imports {module}, undeclared")

    def test_pre_push_hook_runs_ci_and_can_be_bypassed(self):
        """The hook is tracked, so every clone gets it with one config line."""
        repo_root = Path(__file__).resolve().parents[1]
        hook = repo_root / ".githooks" / "pre-push"
        self.assertTrue(hook.is_file(), "the pre-push hook is not tracked in the repo")

        text = hook.read_text(encoding="utf-8")
        self.assertIn("tools/run_ci.py", text)
        self.assertIn("core.hooksPath .githooks", text, "the hook must document how to enable it")
        # An unbypassable hook gets disabled outright the first time it is in the
        # way, so the escape hatch is part of the design — and it must say loudly
        # that nothing was checked.
        self.assertIn("SKIP_CI", text)
        self.assertIn("nothing was verified", text)

    def test_normalize_stage_is_wired_and_skips_in_shape_dlds(self):
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "auto_ip_pipeline.py"
        spec = importlib.util.spec_from_file_location("auto_ip_pipeline", tool_path)
        pipeline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pipeline
        spec.loader.exec_module(pipeline)

        # The stage runs only for an IP whose author document is not in shape.
        self.assertTrue(pipeline.STAGE_CONDITIONS["src_dld_exists"]({"src_dld_exists": "1"}))
        self.assertFalse(pipeline.STAGE_CONDITIONS["src_dld_exists"]({"src_dld_exists": ""}))

        harness = pipeline.load_harness()
        stages = [s["name"] for s in harness["stages"]]
        stages_by_name = {s["name"]: s for s in harness["stages"]}
        self.assertLess(stages.index("normalize_dld"), stages.index("parse_dld"))
        self.assertLess(stages.index("check_normalization"), stages.index("parse_dld"))
        for name in ("normalize_dld", "check_normalization"):
            self.assertEqual(stages_by_name[name]["when"], "src_dld_exists")
        self.assertIn("check_normalization", stages_by_name["normalize_dld"]["gates"])
        self.assertIn("normalize_dld", pipeline.AGENT_PROMPT_BUILDERS)
        self.assertTrue((repo_root / harness["normalization_contract"]).is_file())

        # An in-shape DLD has no .src.md, so its context skips both stages — the
        # pipeline it runs is exactly the one it ran before the stage existed.
        ctx = pipeline.stage_context(harness, "completion_ip", repo_root / "dlds" / "completion_ip_dld.md")
        self.assertEqual(ctx["src_dld_exists"], "")
        self.assertTrue(ctx["src_dld"].endswith("completion_ip_dld.src.md"))

        # Discovery may return an author source — that is how an off-shape IP
        # enters at all — but it is never routed as the DLD itself.
        for source in pipeline.discover_dld_sources():
            self.assertFalse(pipeline.ensure_markdown_dld(source).name.endswith(".src.md"), source)

        # An agent is told to normalize, and told not to sign its own work.
        prompt = pipeline.normalize_prompt("completion_ip")
        self.assertIn("dlds/completion_ip_dld.src.md", prompt)
        self.assertIn("agents/dld_normalization_agent.md", prompt)
        self.assertIn("Do not run --stamp", prompt)

    def test_docx_conversion_becomes_the_author_source(self):
        """A Word DLD is off-shape by construction, so its conversion is the source.

        Word offers no way to write the extractor's markdown conventions, so
        landing the conversion straight on `<ip>_dld.md` and parsing it was always
        optimistic. It now lands on `<ip>_dld.src.md` and normalization produces
        the file the pipeline reads.
        """
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "auto_ip_pipeline.py"
        spec = importlib.util.spec_from_file_location("auto_ip_pipeline", tool_path)
        pipeline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pipeline
        spec.loader.exec_module(pipeline)

        # The .docx itself is never read here — only the routing is under test.
        converted = "# Probe IP\n\nSome prose an engineer wrote in Word.\n"
        original = pipeline.docx_to_markdown
        src = repo_root / "dlds" / "probe_ip_dld.src.md"
        try:
            pipeline.docx_to_markdown = lambda _path: converted
            returned = pipeline.ensure_markdown_dld(repo_root / "dlds" / "probe_ip_dld.docx")

            self.assertEqual(returned.name, "probe_ip_dld.md", "downstream stages must still read the normalized DLD")
            self.assertFalse(returned.exists(), "conversion must not fabricate a normalized DLD")
            self.assertTrue(src.is_file())
            self.assertEqual(src.read_text(encoding="utf-8"), converted)

            # Re-converting an unchanged document must not rewrite the file: that
            # would break the normalization stamp for no reason.
            before = src.stat().st_mtime_ns
            pipeline.ensure_markdown_dld(repo_root / "dlds" / "probe_ip_dld.docx")
            self.assertEqual(src.stat().st_mtime_ns, before)
        finally:
            pipeline.docx_to_markdown = original
            src.unlink(missing_ok=True)

    def test_promotion_requires_a_stamped_normalization(self):
        """--strict is the promotion gate, so it is where the stamp is required."""
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "check_template_coverage", repo_root / "tools" / "check_template_coverage.py"
        )
        coverage_tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(coverage_tool)

        dld = repo_root / "dlds" / "completion_ip_dld.md"
        template = repo_root / "templates" / "completion_ip.template.yaml"
        src = repo_root / "dlds" / "completion_ip_dld.src.md"
        fixture = Path(__file__).resolve().parent / "fixtures" / "normalization" / "completion_ip_dld.src.md"

        # With no author source there is nothing to be faithful to: unchanged.
        self.assertFalse(src.exists())
        _report, errors = coverage_tool.coverage(template, dld, strict=True)
        self.assertEqual(errors, [])

        try:
            src.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
            _report, errors = coverage_tool.coverage(template, dld, strict=True)
            self.assertTrue(any("stamp" in e for e in errors), errors)

            # Reporting mode must stay a report — only promotion is gated.
            _report, lenient = coverage_tool.coverage(template, dld, strict=False)
            self.assertEqual(lenient, [])
        finally:
            src.unlink(missing_ok=True)

    def test_normalization_review_is_a_pause_not_a_failure(self):
        """The agent loop must be winnable, and the human step must read as pending."""
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "auto_ip_pipeline.py"
        spec = importlib.util.spec_from_file_location("auto_ip_pipeline", tool_path)
        pipeline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pipeline
        spec.loader.exec_module(pipeline)

        stages = {s["name"]: s for s in pipeline.load_harness()["stages"]}

        # The agent is gated on the half it can satisfy; gating it on the stamp
        # would make it retry until max_attempts and fail on a human's absence.
        self.assertIn("--no-stamp-check", stages["check_normalization"]["command"])
        self.assertEqual(stages["normalize_dld"]["gates"], ["check_normalization"])

        # The stamp is a separate, required stage that pauses rather than fails.
        stamp_stage = stages["check_normalization_stamp"]
        self.assertNotIn("--no-stamp-check", stamp_stage["command"])
        self.assertTrue(stamp_stage["required"])
        self.assertTrue(stamp_stage["awaiting_human"])
        self.assertEqual(stamp_stage["when"], "src_dld_exists")
        names = [s["name"] for s in pipeline.load_harness()["stages"]]
        self.assertLess(names.index("check_normalization"), names.index("check_normalization_stamp"))
        self.assertLess(names.index("check_normalization_stamp"), names.index("parse_dld"))

        # The exemption is for gates the pipeline cannot satisfy in principle,
        # because a person -- or an agent standing in for one -- must act. One
        # waits on the normalization stamp, two on unresolved findings, and two
        # on a review that has not been run. Anything else claiming the exemption
        # would be turning a real failure into a shrug.
        awaiting = sorted(n for n, s in stages.items() if s.get("awaiting_human"))
        self.assertEqual(
            awaiting,
            [
                "check_model_review_owed",
                "check_normalization_review_owed",
                "check_normalization_stamp",
                "check_review_findings",
                "recheck_review_findings",
            ],
        )

        # review_normalization sits between the mechanical gate and the stamp: no
        # point asking an LLM whether meaning survived a reshape that has already
        # lost a number, and its whole purpose is to inform the human who stamps.
        # Its findings gate must precede the stamp, or an unresolved finding would
        # not block the thing it exists to block.
        self.assertLess(names.index("check_normalization"), names.index("review_normalization"))
        self.assertLess(names.index("review_normalization"), names.index("check_review_findings"))
        self.assertLess(names.index("check_review_findings"), names.index("check_normalization_stamp"))
        self.assertEqual(stages["review_normalization"]["gates"], ["check_normalization_review_owed"])
        # max_attempts 1: re-invoking a fail-only reviewer until it stops finding
        # things is how it gets talked out of its findings.
        self.assertEqual(stages["review_normalization"]["max_attempts"], 1)

    def test_every_agent_stage_can_actually_be_dispatched(self):
        """Each `kind: agent` stage needs a prompt builder, or the run dies on it.

        `run_agent_stage` raises SystemExit for a stage with no builder. Both
        reviewers were wired into the harness, contracted and tested with no
        builder registered, so the pipeline would have hard-exited mid-run the
        first time either of their gates failed — and neither gate could fail
        (see the next test), so nothing ever reached the crash. Two defects
        hiding each other.
        """
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("auto_ip_pipeline", repo_root / "tools" / "auto_ip_pipeline.py")
        pipeline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pipeline
        spec.loader.exec_module(pipeline)

        harness = yaml.safe_load((repo_root / "harness" / "ip_generation_loop.yaml").read_text(encoding="utf-8"))
        agent_stages = [s["name"] for s in harness["stages"] if s.get("kind") == "agent"]
        self.assertTrue(agent_stages)
        for name in agent_stages:
            with self.subTest(stage=name):
                self.assertIn(name, pipeline.AGENT_PROMPT_BUILDERS, f"no prompt builder for agent stage {name}")
                prompt = pipeline.AGENT_PROMPT_BUILDERS[name]("probe_ip")
                self.assertIn("probe_ip", prompt)

    def test_a_reviewers_gate_is_not_satisfied_by_its_own_absence(self):
        """The gate driving a reviewer must fail when no review has been run.

        This is the defect an end-to-end run found: both reviewers were gated on
        `check_review_findings`, which passes when there is no findings file —
        and a findings file only exists after the reviewer runs. The gate was
        green precisely because the stage had never run, so it was skipped for
        every IP, forever. A stage that cannot fire is worse than a missing one:
        it reads as covered.
        """
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("gate", repo_root / "tools" / "check_review_findings.py")
        gate = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = gate
        spec.loader.exec_module(gate)

        harness = yaml.safe_load((repo_root / "harness" / "ip_generation_loop.yaml").read_text(encoding="utf-8"))
        stages = {s["name"]: s for s in harness["stages"]}

        for name in [n for n in stages if n.startswith("review_")]:
            kind = name[len("review_") :]
            with self.subTest(stage=name):
                gate_names = stages[name]["gates"]
                self.assertEqual(len(gate_names), 1)
                command = stages[gate_names[0]]["command"]
                self.assertIn(f"--require {kind}", command, f"{name} is not gated on a review of its own kind")

                # The question that gate asks: an IP with no review owes one.
                # The subject is synthetic on purpose. This used to pick whichever
                # real IP happened to have no findings file, which meant the test
                # broke the moment every IP had been reviewed -- exactly the state
                # a working reviewer stage produces. review_owed() answers from the
                # path alone, so a name that was never registered is a valid and
                # permanently unreviewed subject.
                unreviewed = "never_reviewed_probe_ip"
                self.assertFalse(
                    gate.findings_path(unreviewed, kind).is_file(),
                    "the synthetic subject must have no review, or this proves nothing",
                )
                self.assertIsNotNone(
                    gate.review_owed(unreviewed, kind),
                    "a missing review must be owed, or the reviewer stage is skipped forever",
                )
                # ...while the findings check stays silent about it, which is
                # correct for that question and is exactly why the two differ.
                self.assertEqual(gate.check_ip(unreviewed, kind), [])

    def test_only_actual_reviews_are_named_review(self):
        """A stage named `review_*` must be a review, and nothing else may be.

        `review_template` was neither. It replaced TODO_REVIEW markers and
        promoted a draft — it edited the artifact it was named after. Sitting
        beside two stages that report findings and can never approve, the name
        implied a scrutiny step for promoted templates that has never existed,
        and reading the stage list gave a false account of what the pipeline
        checks. It is now `complete_template`.

        Renaming one stage is worth little if the next one can lie again, so
        this pins the property rather than the name: `review_*` means a
        fail-only agent stage, gated on findings, with a contract of its own.
        """
        repo_root = Path(__file__).resolve().parents[1]
        harness = yaml.safe_load((repo_root / "harness" / "ip_generation_loop.yaml").read_text(encoding="utf-8"))
        stages = {s["name"]: s for s in harness["stages"]}

        reviews = [n for n in stages if n.startswith("review_")]
        self.assertEqual(sorted(reviews), ["review_model", "review_normalization"])

        for name in reviews:
            stage = stages[name]
            subject = name[len("review_") :]
            with self.subTest(stage=name):
                self.assertEqual(stage["kind"], "agent", f"{name} must be an agent stage")
                # Gated on whether a review of its own kind exists -- never on
                # the findings check, which passes when no review has been run
                # and so is satisfied by this stage never having fired.
                gates = stage.get("gates", [])
                self.assertEqual(len(gates), 1, f"{name} should have exactly one gate")
                command = stages[gates[0]]["command"]
                self.assertIn(
                    f"--require {subject}",
                    command,
                    f"{name}'s gate must ask whether its own review exists, not whether findings are resolved",
                )
                # One attempt: re-invoking a fail-only reviewer until it stops
                # finding things is how it gets talked out of its findings.
                self.assertEqual(stage["max_attempts"], 1, f"{name} must not be retried")
                # Its own contract, naming the asymmetry it runs under.
                contract = harness.get(f"{subject}_review_contract")
                self.assertIsNotNone(contract, f"{name} has no declared contract")
                text = (repo_root / contract).read_text(encoding="utf-8")
                self.assertIn("may never pass", text, f"{contract} must state the fail-only rule")

        # And the converse: a stage that edits the artifact it is named for is
        # not a review, whatever it is called.
        self.assertIn("complete_template", stages)
        self.assertEqual(stages["complete_template"]["gates"], ["check_dld_coverage", "lint_template"])

    def test_emitted_transition_findings_are_readable_by_the_gate(self):
        """A disagreement the checker finds must become a finding that blocks.

        The two tools have to agree on three things or the mechanism is theatre:
        the closed class vocabulary, the required fields, and the hash of every
        subject file. Get the last one wrong by a newline and the review reads as
        stale the moment it is written — green gate, unrecorded disagreement.

        It must also refuse to overwrite findings it did not write. A reviewer's
        findings are somebody's reading of the model, and a tool that regenerates
        a file must not delete them.
        """
        repo_root = Path(__file__).resolve().parents[1]

        def load(name):
            spec = importlib.util.spec_from_file_location(name, repo_root / "tools" / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module

        checker, gate = load("check_declared_transitions"), load("check_review_findings")

        ip = "completion_ip"
        result = {
            "ip": ip,
            "undeclared": [("bank_scheduler", "RETURN_DATA", "SCHED_IDLE")],
            "never_taken": [("bank_scheduler", "RETURN_DATA", "RETURN_DATA")],
            "declared": [("bank_scheduler", "RETURN_DATA", "PICK_BANK")],
            "states_without_exit": [],
        }
        path = repo_root / "reviews" / f"{ip}.model.findings.yaml"
        original = path.read_text(encoding="utf-8") if path.is_file() else None
        try:
            # A real IP is needed so the subject hashes resolve, but this test is
            # about the two tools agreeing on a file they round-trip -- not about
            # whether that IP has been reviewed. Once it had a real review,
            # emit_findings correctly refused to overwrite it and the test failed
            # on the tool being right. So the file is moved out of the way for the
            # duration and restored in `finally`.
            path.unlink(missing_ok=True)

            _, count, error = checker.emit_findings(result, ip)
            self.assertIsNone(error)
            self.assertEqual(count, 2)

            # The gate parses it, accepts every class and field, and does not
            # call it stale — the hashes agree with the live files.
            data, errors = gate.load_findings(path)
            self.assertEqual(errors, [], "the gate rejected findings the checker wrote")
            self.assertIsNone(gate.review_is_stale(data, ip, "model"))
            self.assertEqual([f["id"] for f in data["findings"]], ["T1", "T2"])

            # And it blocks: unresolved is the whole point.
            self.assertTrue(any("T1" in e for e in gate.check_ip(ip, "model")))

            # A reviewer's finding in the same file stops regeneration dead.
            import yaml as _yaml

            doc = _yaml.safe_load(path.read_text(encoding="utf-8"))
            doc["findings"].append(dict(doc["findings"][0], id="M42"))
            path.write_text(_yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
            _, count, error = checker.emit_findings(result, ip)
            self.assertIsNotNone(error)
            self.assertIn("M42", error)
            self.assertIn("M42", path.read_text(encoding="utf-8"), "it deleted a reviewer's finding")
        finally:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(original, encoding="utf-8")

    def test_a_new_off_shape_dld_can_enter_the_pipeline(self):
        """An IP whose only file is a .src.md must still be discovered and routed.

        This is the case the stage exists for — a DLD arriving in some other
        shape, for an IP that has nothing else yet — and it was invisible:
        discovery globbed only `*_dld.md` and `*_dld.docx`, so the pipeline could
        not see the document it was built to reshape.
        """
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "auto_ip_pipeline.py"
        spec = importlib.util.spec_from_file_location("auto_ip_pipeline", tool_path)
        pipeline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pipeline
        spec.loader.exec_module(pipeline)

        src = repo_root / "dlds" / "probe_only_ip_dld.src.md"
        self.assertFalse(src.exists())
        try:
            src.write_text("# Probe\n\nProse an engineer wrote.\n", encoding="utf-8")

            discovered = pipeline.discover_dld_sources()
            self.assertIn(src, discovered)

            # ...and it is routed as an input, never as the DLD itself: returning
            # the source here would point every later stage at the author's file.
            self.assertEqual(pipeline.ensure_markdown_dld(src).name, "probe_only_ip_dld.md")
            self.assertEqual(pipeline.ip_stem(src), "probe_only_ip_dld")
            self.assertEqual(pipeline.src_dld_path(src), src)

            # Naming the normalized DLD is the natural thing to type; it does not
            # exist yet, and refusing the argument would hide the real answer.
            requested = pipeline.resolve_requested_dld(repo_root / "dlds" / "probe_only_ip_dld.md")
            self.assertEqual(requested, src)
        finally:
            src.unlink(missing_ok=True)

    def test_each_ip_is_processed_once_however_many_files_it_has(self):
        """.docx, .src.md and .md are one IP, not three runs of it."""
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "auto_ip_pipeline.py"
        spec = importlib.util.spec_from_file_location("auto_ip_pipeline", tool_path)
        pipeline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pipeline
        spec.loader.exec_module(pipeline)

        src = repo_root / "dlds" / "completion_ip_dld.src.md"
        self.assertFalse(src.exists())
        try:
            src.write_text("# Completion\n\nAuthor's original.\n", encoding="utf-8")
            discovered = pipeline.discover_dld_sources()
            stems = [pipeline.ip_stem(p) for p in discovered]
            self.assertEqual(len(stems), len(set(stems)), "an IP was queued more than once")
            # The author source outranks the file derived from it.
            self.assertIn(src, discovered)
            self.assertNotIn(repo_root / "dlds" / "completion_ip_dld.md", discovered)
        finally:
            src.unlink(missing_ok=True)

    def test_change_detection_covers_the_author_source(self):
        """Editing the .src.md must re-trigger its IP, or a corrected source is ignored."""
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "auto_ip_pipeline.py"
        spec = importlib.util.spec_from_file_location("auto_ip_pipeline", tool_path)
        pipeline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pipeline
        spec.loader.exec_module(pipeline)

        dld = repo_root / "dlds" / "completion_ip_dld.md"
        src = pipeline.src_dld_path(dld)
        self.assertFalse(src.exists(), f"{src.name} is not expected in the repo")

        without_source = pipeline.source_fingerprint(dld)
        self.assertEqual(without_source, pipeline.sha256(dld))
        try:
            src.write_text("draft one\n", encoding="utf-8")
            with_source = pipeline.source_fingerprint(dld)
            self.assertNotEqual(with_source, without_source)
            src.write_text("draft two\n", encoding="utf-8")
            self.assertNotEqual(pipeline.source_fingerprint(dld), with_source)
        finally:
            src.unlink(missing_ok=True)

    def test_template_diff_classifies_changes(self):
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "diff_template.py"
        spec = importlib.util.spec_from_file_location("diff_template", tool_path)
        diff = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        # Register before exec so @dataclass can resolve annotations via sys.modules.
        sys.modules[spec.name] = diff
        spec.loader.exec_module(diff)

        import yaml

        base = yaml.safe_load(
            # A fixture template, not a live IP: this test mutates specific
            # queues and FSMs by name, so it needs a document whose shape is
            # fixed independently of which IPs the repo currently ships.
            (Path(__file__).resolve().parent / "fixtures" / "templates" / "mailbox_ip.template.yaml").read_text(
                encoding="utf-8"
            )
        )
        after = copy.deepcopy(base)
        for q in after["queues"]:
            if q["name"] == "message_fifo":
                q["depth"] = 16
        for f in after["fsm_processes"]:
            if f["name"] == "doorbell":
                f["states"].append("COALESCE")

        # Identical templates -> no changes.
        self.assertEqual(diff.diff_templates(base, base), [])

        changes = diff.diff_templates(base, after)
        details = {c.detail: c.radius for c in changes}
        depth_change = next(d for d in details if "message_fifo`.depth" in d)
        state_change = next(d for d in details if "doorbell` states" in d)
        self.assertEqual(details[depth_change], diff.SURGICAL)
        self.assertEqual(details[state_change], diff.STRUCTURAL)
        # A structural change dominates the overall radius.
        self.assertEqual(diff.overall_radius(changes), diff.STRUCTURAL)

    def test_model_provenance_tracks_templates(self):
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "check_model_provenance.py"
        spec = importlib.util.spec_from_file_location("check_model_provenance", tool_path)
        tool = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(tool)

        # Every promoted template is stamped and in sync with its model baseline.
        self.assertEqual(tool.check(), [], "a model template has drifted from its recorded baseline")

        # --stamp-all would assert "every model was amended against its current
        # template" — a claim only the amend flow earns — so once baselines
        # exist it must refuse rather than silence the gate in bulk.
        self.assertEqual(sorted(tool.stamp_all_blockers()), sorted(tool.promoted_ips()))
        before = tool.BASELINES_PATH.read_text(encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(tool.main(["--stamp-all"]), 1)
        self.assertIn("refusing --stamp-all", stderr.getvalue())
        self.assertEqual(tool.BASELINES_PATH.read_text(encoding="utf-8"), before)

    def test_overview_docx_tracks_markdown(self):
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "check_overview_sync.py"
        spec = importlib.util.spec_from_file_location("check_overview_sync", tool_path)
        tool = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(tool)

        # The docx must carry a source stamp matching the current Markdown, so a
        # Markdown edit that forgets to re-sync the Word copy fails here.
        self.assertEqual(tool.check(), [], "project_overview.docx has drifted from project_overview.md")

    @staticmethod
    def _synthetic_subsystem(members: str, connections: str) -> str:
        """A subsystem template built from whole cloth, not seeded from a live one.

        These two tests used to read templates/dma_subsystem.template.yaml. That
        made the checker's proof depend on a particular IP existing, and when
        dma_subsystem and mailbox_irq_subsystem were retired the proof went with
        them -- leaving a wiring checker that validate_dld_flow.py still runs
        against whatever subsystems exist, which is now none.

        The subsystem is named completion_ip so that profile.model_file() resolves to a
        real model file. That is what lets the "does not instantiate member model"
        branch fire at all; without an existing subsystem model the checker stops
        at "no subsystem model file" and never reaches it.
        """
        return (
            "ip:\n  name: completion_ip\n  description: synthetic wiring fixture\n"
            "subsystem:\n  members:\n"
            + members
            + "  connections:\n"
            + connections
            + "fsm_processes:\n  - name: real_glue_fsm\n"
        )

    def _wiring_checker(self):
        repo_root = Path(__file__).resolve().parents[1]
        checker_path = repo_root / "tools" / "check_subsystem_wiring.py"
        spec = importlib.util.spec_from_file_location("check_subsystem_wiring", checker_path)
        checker = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(checker)
        return repo_root, checker

    def test_subsystem_wiring_check_accepts_well_formed_wiring(self):
        """The checker must not false-positive, which is the half that can rot silently.

        The repo currently contains no subsystem templates at all, so
        discover_subsystem_templates returns nothing and validate_dld_flow.py's
        wiring stage checks nothing. A test that walked the repo's subsystems
        would now pass by having no work to do; this one gives it work.
        """
        repo_root, checker = self._wiring_checker()

        self.assertEqual(
            checker.discover_subsystem_templates(repo_root),
            [],
            "a subsystem template was added -- give it a real wiring test rather than relying on this one",
        )

        good = self._synthetic_subsystem(
            members="    - {ip: arbitration_ip, model: ArbitrationIpModel, role: scheduler}\n",
            connections="    - {from: real_glue_fsm, to: arbitration_ip.enqueue, payload: command, ack: none}\n",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "completion_ip.template.yaml"
            target.write_text(good, encoding="utf-8")
            errors = checker.check_file(repo_root, target)
        self.assertEqual(
            [e for e in errors if "does not instantiate" not in e],
            [],
            "the checker rejects wiring that is actually well formed",
        )

    def test_subsystem_wiring_check_detects_bad_wiring(self):
        repo_root, checker = self._wiring_checker()

        broken = self._synthetic_subsystem(
            members=(
                "    - {ip: ghost_ip, model: GhostIpModel, role: missing}\n"
                "    - {ip: arbitration_ip, model: ArbitrationIpModel, role: scheduler}\n"
            ),
            connections=(
                "    - {from: real_glue_fsm, to: arbitration_ip.enqueue_nonexistent, payload: command, ack: none}\n"
                "    - {from: bogus_fsm, to: arbitration_ip.enqueue, payload: command, ack: none}\n"
            ),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "completion_ip.template.yaml"
            target.write_text(broken, encoding="utf-8")
            errors = "\n".join(checker.check_file(repo_root, target))

        self.assertIn("member `ghost_ip`: no promoted template", errors)
        self.assertIn("does not instantiate member model `GhostIpModel`", errors)
        self.assertIn("`enqueue_nonexistent` not found in arbitration_ip.py", errors)
        self.assertIn("`bogus_fsm`: not a glue FSM of completion_ip", errors)

    def test_ip_logging_writes_ip_tagged_run_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "run.log"
            env = simpy.Environment()
            model = ArbitrationIpModel(
                env,
                {"T0": 1},
                scan_latency=1,
                issue_latency=1,
                pending_count_latency=1,
                burst_read_latency=1,
                burst_calc_latency=1,
                burst_debit_latency=1,
                log_level="INFO",
                log_file=str(log_file),
            )
            model.enqueue(Command("log0", "READ", tenant_id="T0"))
            env.run(until=20)

            text = log_file.read_text(encoding="utf-8")
            self.assertIn("[arbitration_ip]", text)
            self.assertIn("INFO", text)
            self.assertIn("log0", text)
            logging.shutdown()

    def test_ip_logging_rejects_unknown_level(self):
        with self.assertRaises(ValueError):
            get_ip_logger("bad_ip", "LOUD")


class TestDldNormalizationGate(unittest.TestCase):
    """Fidelity gate for the proposed `normalize_dld` stage.

    See docs/proposals/normalize_dld_stage.md. The stage itself is not wired into
    the pipeline; this gate is built and calibrated first, deliberately, so the
    tokenizer is proved against real DLDs before any LLM output exists.
    """

    @staticmethod
    def _gate():
        repo_root = Path(__file__).resolve().parents[1]
        tool_path = repo_root / "tools" / "check_dld_normalization.py"
        spec = importlib.util.spec_from_file_location("check_dld_normalization", tool_path)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        return gate

    @staticmethod
    def _real_dld():
        # A fixture, not a live IP. These tests feed the tokenizers a real,
        # structurally rich DLD and then damage it in specific ways, so they are
        # coupled to its prose -- its FSM names, its state names, its headings.
        # Pointing them at whichever IP the repo happens to ship made them break
        # when mailbox_ip was retired. The document is kept here for the same
        # reason completion_ip's normalization pair is: the tokenizers need real
        # input, and that input should not be at the mercy of the IP roster.
        fixtures = Path(__file__).resolve().parent / "fixtures" / "normalization"
        return (fixtures / "mailbox_ip_dld.md").read_text(encoding="utf-8")

    @staticmethod
    def _fixture_pair():
        """A whole document reshaped by hand: off-shape source, extractor-shaped result.

        Calibration proves the tokenizer does not false-positive on an identity
        rewrite, which is a weaker claim than it sounds — every check passes
        trivially when nothing moved. This pair is the real exercise: different
        headings, states in a table, timing and blocking behavior in prose, no
        code formatting anywhere, and front matter that belongs in no section.
        """
        fixtures = Path(__file__).resolve().parent / "fixtures" / "normalization"
        return (
            (fixtures / "completion_ip_dld.src.md").read_text(encoding="utf-8"),
            (fixtures / "completion_ip_dld.md").read_text(encoding="utf-8"),
        )

    def test_every_dld_passes_identity_normalization(self):
        """Calibration: rewriting a DLD to itself must never trip a check."""
        gate = self._gate()
        for ip in gate.all_ips():
            text = gate.normalized_path(ip).read_text(encoding="utf-8")
            self.assertEqual(
                gate.check_ip(ip, text, text, require_stamp=False),
                [],
                f"{ip}: identity normalization is not clean — the tokenizer false-positives",
            )

    def test_tokenizers_read_real_dld_content(self):
        """A gate that parses nothing would also pass calibration trivially."""
        gate = self._gate()
        text = self._real_dld()

        self.assertIn("2 ns", gate.measurements(text))
        self.assertIn("500 mhz", gate.measurements(text))

        found = gate.identifiers(text)
        for state in ("REJECT_FULL", "WAIT_SW_CLEAR", "CHECK_SPACE"):
            self.assertIn(state, found)
        # Prose acronyms are not identifiers, or restructuring prose would fail.
        self.assertNotIn("FIFO", found)
        self.assertNotIn("IPC", found)

        # "one cycle" and "1 cycle" must compare equal across a prose->table rewrite.
        self.assertEqual(gate.measurements("takes one cycle"), gate.measurements("takes 1 cycle"))

    def test_gate_catches_altered_number_not_merely_missing_one(self):
        """The most dangerous edit: a retyped timing in a doc stating it elsewhere.

        `2 ns` appears many times in the DLD, so retyping one as `3 ns` leaves
        the value present and a lost-value check sees nothing. Only checking the
        other direction — a value that appears from nowhere — catches it.
        """
        gate = self._gate()
        source = self._real_dld()
        mutated = source.replace("Accept message: 1 cycle = 2 ns.", "Accept message: 1 cycle = 3 ns.", 1)

        self.assertNotEqual(source, mutated)
        self.assertIn("2 ns", gate.measurements(mutated))  # still present, hence invisible to a lost-check
        errors = gate.check_measurements(source, mutated)
        self.assertTrue(any("invented" in e and "3 ns" in e for e in errors), errors)

    def test_gate_catches_dropped_and_invented_identifiers(self):
        gate = self._gate()
        source = self._real_dld()

        dropped = source.replace("- `REJECT_FULL`\n", "", 1)
        self.assertTrue(any("dropped" in e and "REJECT_FULL" in e for e in gate.check_identifiers(source, dropped)))

        invented = source.replace("- `REJECT_FULL`\n", "- `REJECT_FULL`\n- `COALESCE_PENDING`\n", 1)
        self.assertTrue(
            any("invented" in e and "COALESCE_PENDING" in e for e in gate.check_identifiers(source, invented)),
        )

    def test_gate_catches_fsm_topology_and_count_drift(self):
        gate = self._gate()
        source = self._real_dld()

        recounted = source.replace("Total FSM/processes: 5.", "Total FSM/processes: 6.", 1)
        self.assertTrue(any("count" in e for e in gate.check_fsm_parity(source, recounted)))

        start, end = source.index("### 6.4 Doorbell FSM"), source.index("### 6.5 Interrupt Notify FSM")
        deleted = source[:start] + source[end:]
        self.assertTrue(any("doorbell" in e for e in gate.check_fsm_parity(source, deleted)))

    def test_gate_refuses_a_wait_model_conjured_for_a_silent_interface(self):
        """A wait model invented where the source states no blocking behavior."""
        gate = self._gate()
        silent = """### 4.5 Debug Observation Interface

Fields:

- `probe_id`
"""
        conjured = (
            silent
            + """
Wait model:

- Mode: `wait_for_response`
- Requester: peer
- Waits in: `register_access.READ_STATUS`
- Resumes on: `register_access_complete`
"""
        )
        errors = gate.check_wait_model_provenance(silent, conjured)
        self.assertTrue(any("wait model" in e for e in errors), errors)

        # Where the source *does* state blocking behavior, the same block is fine.
        stated = silent + "\nThe requester blocks until the probe response returns.\n"
        self.assertEqual(gate.check_wait_model_provenance(stated, conjured), [])

    def test_gate_catches_silently_dropped_prose(self):
        gate = self._gate()
        source = self._real_dld()
        start, end = source.index("Some mailbox processes are naturally parallel"), source.index("## 4. Interfaces")
        truncated = source[:start] + source[end:]

        self.assertTrue(any("neither" in e for e in gate.check_unplaced_accounting(source, truncated)))

        # Preserved verbatim under the Unplaced heading, the same content is accounted for.
        rescued = truncated + f"\n{gate.UNPLACED_HEADING}\n\n" + source[start:end]
        self.assertEqual(gate.check_unplaced_accounting(source, rescued), [])

    def test_gate_allows_a_structure_only_reshape(self):
        """The gate must not simply fail everything: renumbering is legal."""
        gate = self._gate()
        source = self._real_dld()
        reshaped = re.sub(r"^### 6\.(\d) ", lambda m: f"### 7.{m.group(1)} ", source, flags=re.MULTILINE)
        reshaped = re.sub(r"^- `([A-Z_]+)`$", r"* `\1`", reshaped, flags=re.MULTILINE)

        self.assertNotEqual(source, reshaped)
        self.assertEqual(gate.check_ip("mailbox_ip", source, reshaped, require_stamp=False), [])

    def test_fractional_cycle_times_survive_extraction(self):
        """A clock that is not a whole number of nanoseconds must not lose timings.

        Every DLD in the repo runs at 500 MHz — exactly 2 ns/cycle — so nothing
        ever exercised a fractional cycle time. At 800 MHz (1.25 ns) the
        extractor stored `cycle_time_ns: 1` and silently dropped every operation
        whose nanosecond figure was fractional: six of eight timings vanished
        from the template with nothing flagged.
        """
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("dld_to_template", repo_root / "tools" / "dld_to_template.py")
        extractor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(extractor)

        clock_mhz, cycle_ns, _gaps = extractor.extract_clock("Core clock: 800 MHz.\nCycle time: 1.25 ns.\n")
        self.assertEqual(clock_mhz, 800)
        self.assertEqual(cycle_ns, 1.25)

        row = [
            "| FSM/process | Runs as | Delay model |",
            "| --- | --- | --- |",
            "| Bank Scheduler FSM | Parallel process | Pick the next bank: 3 cycles = 3.75 ns. "
            "SRAM read access: 4 cycles = 5 ns. |",
        ]
        ops = extractor.extract_timing_table(row, ["bank_scheduler"])["bank_scheduler"]
        self.assertEqual(
            [(op["name"], op["cycles"], op["ns"]) for op in ops],
            [("pick_the_next_bank", 3, 3.75), ("sram_read_access", 4, 5)],
        )
        # A whole number stays an int, so existing templates are not churned to 5.0.
        self.assertIsInstance(ops[1]["ns"], int)

    def test_timing_coherence_accepts_fractional_but_still_catches_slips(self):
        """The gate demanded an integer ns/cycle, so it rejected correct values."""
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("template_lint", repo_root / "tools" / "template_lint.py")
        lint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lint)

        def timing(cycle_ns, ns):
            return {
                "timing_model": {
                    "clock_mhz": 800,
                    "cycle_time_ns": cycle_ns,
                    "fsm_process_delays": [{"fsm": "x", "operations": [{"name": "op", "cycles": 2, "ns": ns}]}],
                    "end_to_end_paths": [],
                }
            }

        self.assertEqual(lint.timing_coherence_errors(timing(1.25, 2.5)), [])
        # The truncation the old extractor produced is now itself a lint failure.
        self.assertTrue(any("1.25" in e for e in lint.timing_coherence_errors(timing(1, 2))))
        # And a genuine transcription slip still fails.
        self.assertTrue(any("op" in e for e in lint.timing_coherence_errors(timing(1.25, 3.0))))

    def test_hand_normalized_off_shape_document_passes_every_check(self):
        """The gate must accept a real reshape, or the stage it guards is unusable."""
        gate = self._gate()
        source, normalized = self._fixture_pair()

        self.assertNotIn("States:", source)  # the source really is off-shape
        self.assertIn("States:", normalized)
        self.assertIn(gate.UNPLACED_HEADING, normalized)
        # ...and marks up none of its signal names, unlike the normalized file.
        self.assertNotIn("cpl_ready", gate.identifiers(source))
        self.assertIn("cpl_ready", gate.identifiers(normalized))

        self.assertEqual(gate.check_ip("completion_ip", source, normalized, require_stamp=False), [])

    def test_normalized_fixture_is_what_the_extractor_actually_reads(self):
        """Pin the fixture to reality: it must extract like the production DLD.

        Without this, the fixture could drift into a document that satisfies the
        fidelity gate while being useless to the parser downstream — which would
        make the test above prove nothing worth proving.
        """
        repo_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("dld_to_template", repo_root / "tools" / "dld_to_template.py")
        extractor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(extractor)

        _source, normalized = self._fixture_pair()
        production = (repo_root / "dlds" / "completion_ip_dld.md").read_text(encoding="utf-8")

        def shape(text):
            lines = text.splitlines()
            return (
                [f["name"] for f in extractor.extract_fsms(lines)],
                [f["states"] for f in extractor.extract_fsms(lines)],
                [i["name"] for i in extractor.extract_interfaces(lines)],
            )

        self.assertEqual(shape(normalized), shape(production))

    def test_conservation_ignores_markup_but_not_invention(self):
        """Backticks are shape. Adding or removing them is not a content change.

        Comparing *marked* identifier sets made an author who writes plain prose
        fail with dozens of invented-name errors — for a document in which every
        name was present. The invention check now asks whether the name appears in
        the source at all, which is what "hallucinated" actually means.
        """
        gate = self._gate()
        dld = self._real_dld()

        self.assertEqual(gate.check_identifiers(dld.replace("`", ""), dld), [])
        self.assertEqual(gate.check_identifiers(dld, dld.replace("`", "")), [])

        conjured = dld.replace("- `REJECT_FULL`", "- `REJECT_FULL`\n- `COALESCE_PENDING`", 1)
        self.assertTrue(any("COALESCE_PENDING" in e for e in gate.check_identifiers(dld, conjured)))

        # A wait mode is the extractor's vocabulary, not a name: writing the
        # `Mode:` line for prose-stated blocking must not read as invention.
        stated = "### 4.1 Doorbell Interface\n\nThe requester blocks until the doorbell is acknowledged.\n"
        shaped = stated + "\nWait model:\n\n- Mode: `wait_for_ack_inline`\n"
        self.assertEqual(gate.check_identifiers(stated, shaped), [])

    def test_wait_model_provenance_finds_its_section_by_content(self):
        """Heading matching failed on exactly the documents the stage is for.

        An off-shape source is one that does not use the extractor's headings, so
        a section called "3.1 Request port" matched no normalized "Request
        Interface" and the search fell back to the whole document — where some
        form of "wait" appears in nearly any DLD. The check passed on evidence
        from an unrelated section, which is no check at all.
        """
        gate = self._gate()
        source = """## 3.1 Request port

The producer presents a request. If the queue is full the controller drops
req_ready and the requester must hold its request until space appears.

## 3.2 Debug port

Counters are readable at any time.
"""
        # Wording is preserved, as the contract requires — that is the signal
        # content matching relies on. Only the heading differs.
        normalized = """### 3.1 Request Interface

The producer presents a request.

Wait model:

- Mode: `wait_for_ack_inline`
- Note: if the queue is full the controller drops req_ready and the requester
  must hold its request until space appears.
"""
        title, body = gate.match_source_section("3.1 Request Interface", normalized, source)
        self.assertIn("Request port", title, "content matching failed across differing headings")
        self.assertIn("until space appears", body)
        self.assertEqual(gate.check_wait_model_provenance(source, normalized), [])

        # A silent interface must still be caught, and not rescued by prose
        # elsewhere in the document.
        silent = source.replace(
            "If the queue is full the controller drops\nreq_ready and the requester must hold its "
            "request until space appears.",
            "Requests are presented on this port.",
        )
        quiet_normalized = normalized.replace(
            "- Note: if the queue is full the controller drops req_ready and the requester\n"
            "  must hold its request until space appears.\n",
            "",
        )
        self.assertTrue(
            gate.check_wait_model_provenance(silent, quiet_normalized),
            "a wait model conjured for a silent interface must fail",
        )

    def test_blocking_language_is_matched_as_whole_words(self):
        """`req_ready` is a signal name, and "this block" is a noun."""
        gate = self._gate()
        self.assertEqual(gate.blocking_hits("| req_ready | out | controller can accept |"), 0)
        self.assertEqual(gate.blocking_hits("It is the block that decides bandwidth."), 1)  # still a word
        self.assertGreaterEqual(gate.blocking_hits("The requester blocks until the response returns."), 2)

        evidence = gate.blocking_evidence(
            "| req_ready | out | accept |\nThe requester stalls until an entry frees up.\n"
        )
        self.assertIn("stalls until", evidence)

    def test_review_report_gives_the_stamper_something_to_read(self):
        """The stamp asks for a judgement; the report is the material for it."""
        gate = self._gate()
        source, normalized = self._fixture_pair()
        report = gate.build_report("completion_ip", source, normalized, [])

        self.assertIn("Where each source section landed", report)
        self.assertIn("Unplaced source content", report)
        # The map must actually pair sections, not list them.
        self.assertIn("| 1 Why This Block Exists |", report)
        # And it must say plainly what it cannot establish.
        self.assertIn("cannot tell you", report)

    def test_gate_catches_a_state_attached_to_the_wrong_fsm(self):
        """Conservation cannot see this; the extractor can.

        Moving a state between FSMs preserves every number and every name, so the
        proposal lists it as the gate's honest limit. For states specifically it
        is now caught, because the state sets are compared per FSM.
        """
        gate = self._gate()
        dld = self._real_dld()
        # REJECT_FULL belongs to message_push; hang it on register_access instead.
        moved = dld.replace("- `REJECT_FULL`\n", "", 1).replace(
            "- `ACCESS_ERROR`", "- `ACCESS_ERROR`\n- `REJECT_FULL`", 1
        )

        self.assertNotEqual(dld, moved)
        self.assertEqual(gate.check_measurements(dld, moved), [])
        self.assertEqual(gate.check_identifiers(dld, moved), [])
        errors = gate.check_state_parity(dld, moved)
        self.assertTrue(any("REJECT_FULL" in e for e in errors), errors)

    def test_retitling_a_heading_is_not_content_loss(self):
        """Renumbering and retitling headings is the first permitted operation."""
        gate = self._gate()
        dld = self._real_dld()
        retitled = dld.replace("## 4. Interfaces", "## 5 Signal Interfaces", 1)

        self.assertEqual(gate.check_unplaced_accounting(dld, retitled), [])
        # Deleting the section's content is still loss, heading or no heading.
        start, end = dld.index("## 4. Interfaces"), dld.index("## 5. Message Flow")
        self.assertTrue(gate.check_unplaced_accounting(dld, dld[:start] + dld[end:]))

    def test_unstamped_normalization_is_not_trusted(self):
        """Mechanical checks cannot prove meaning survived; a human signs that."""
        gate = self._gate()
        text = self._real_dld()
        errors = gate.check_ip("no_such_ip", text, text, require_stamp=True)
        self.assertTrue(any("stamp" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
