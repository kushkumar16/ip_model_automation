import contextlib
import copy
import importlib.util
import io
import logging
import sys
import tempfile
import unittest
from pathlib import Path

import simpy

from ip_model_automation.common import get_ip_logger
from ip_model_automation.ip import IP_ARTIFACTS, ArbitrationIpModel, Command, list_ips, resolve_artifacts


class TestIpRegistryAndLayout(unittest.TestCase):
    def test_registry_has_all_ips_and_artifacts(self):
        self.assertEqual(len(tuple(list_ips())), 11)
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
            "axi_interconnect_ip.py",
            "common.py",
            "completion_ip.py",
            "dma_subsystem.py",
            "gdma_ip.py",
            "i3c_ip.py",
            "interrupt_controller_ip.py",
            "ip.py",
            "mailbox_ip.py",
            "mailbox_irq_subsystem.py",
            "spi_master_ip.py",
            "timer_ip.py",
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
        self.assertEqual(len(templates), 11)
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
        self.assertIn("templates: 11", text)

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

        base = yaml.safe_load((repo_root / "templates" / "mailbox_ip.template.yaml").read_text(encoding="utf-8"))

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
        base = yaml.safe_load((repo_root / "templates" / "mailbox_ip.template.yaml").read_text(encoding="utf-8"))

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
        prompt = pipeline.amend_prompt("mailbox_ip")
        self.assertIn("mailbox_ip", prompt)

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

        base = yaml.safe_load((repo_root / "templates" / "mailbox_ip.template.yaml").read_text(encoding="utf-8"))
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

    def test_subsystem_wiring_check_passes_for_repo(self):
        repo_root = Path(__file__).resolve().parents[1]
        checker_path = repo_root / "tools" / "check_subsystem_wiring.py"
        spec = importlib.util.spec_from_file_location("check_subsystem_wiring", checker_path)
        checker = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(checker)

        templates = checker.discover_subsystem_templates(repo_root)
        self.assertEqual(
            {path.name for path in templates},
            {"dma_subsystem.template.yaml", "mailbox_irq_subsystem.template.yaml"},
        )
        for template in templates:
            self.assertEqual(checker.check_file(repo_root, template), [])

    def test_subsystem_wiring_check_detects_bad_wiring(self):
        repo_root = Path(__file__).resolve().parents[1]
        checker_path = repo_root / "tools" / "check_subsystem_wiring.py"
        spec = importlib.util.spec_from_file_location("check_subsystem_wiring", checker_path)
        checker = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(checker)

        text = (repo_root / "templates" / "dma_subsystem.template.yaml").read_text(encoding="utf-8")
        broken = text.replace("ip: gdma_ip, model: GdmaIpModel", "ip: ghost_ip, model: GhostIpModel")
        broken = broken.replace("to: arbitration_ip.enqueue", "to: arbitration_ip.enqueue_nonexistent")
        broken = broken.replace(
            "from: backpressure_monitor, to: gdma_ip.set_memory_ready",
            "from: bogus_fsm, to: gdma_ip.set_memory_ready",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "dma_subsystem.template.yaml"
            target.write_text(broken, encoding="utf-8")
            errors = "\n".join(checker.check_file(repo_root, target))
        self.assertIn("member `ghost_ip`: no promoted template", errors)
        self.assertIn("does not instantiate member model `GhostIpModel`", errors)
        self.assertIn("`enqueue_nonexistent` not found in arbitration_ip.py", errors)
        self.assertIn("`bogus_fsm`: not a glue FSM of dma_subsystem", errors)

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


if __name__ == "__main__":
    unittest.main()
