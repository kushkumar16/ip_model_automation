import copy
import importlib.util
import logging
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
