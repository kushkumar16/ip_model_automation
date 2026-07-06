import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


def load_tool():
    repo_root = Path(__file__).resolve().parents[1]
    tool_path = repo_root / "tools" / "run_experiments.py"
    spec = importlib.util.spec_from_file_location("run_experiments", tool_path)
    tool = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # The tool uses dataclasses with postponed annotations; dataclass field
    # resolution needs the module present in sys.modules during exec.
    sys.modules["run_experiments"] = tool
    spec.loader.exec_module(tool)
    return tool


class TestRunExperiments(unittest.TestCase):
    def test_experiment_registry_is_wellformed(self):
        tool = load_tool()
        self.assertGreaterEqual(len(tool.EXPERIMENTS), 6)
        for experiment in tool.EXPERIMENTS.values():
            self.assertTrue(experiment.description)
            self.assertGreaterEqual(len(experiment.values), 2)
            self.assertTrue(callable(experiment.runner))

    def test_arbitration_burst_credit_sweep_writes_csv_and_shows_credit_ceiling(self):
        tool = load_tool()
        experiment = tool.EXPERIMENTS["arbitration_burst_credit"]
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            rows = tool.run_experiment(experiment, output_dir)

            csv_path = output_dir / "arbitration_burst_credit.csv"
            self.assertTrue(csv_path.is_file())
            with csv_path.open(encoding="utf-8") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(len(csv_rows), len(experiment.values))

            # Burst credit is a hard ceiling: issued == min(credit, queued=12).
            by_credit = {row["burst_credit"]: row["issued_commands"] for row in rows}
            self.assertEqual(by_credit[1], 1)
            self.assertEqual(by_credit[2], 2)
            self.assertEqual(by_credit[4], 4)
            self.assertEqual(by_credit[16], 12)

            summary = tool.write_summary({experiment.name: rows}, output_dir)
            text = summary.read_text(encoding="utf-8")
            self.assertIn("## arbitration_burst_credit", text)
            self.assertIn("| burst_credit |", text)


if __name__ == "__main__":
    unittest.main()
