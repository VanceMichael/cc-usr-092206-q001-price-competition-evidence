"""报告与端到端演示：三份报告可生成，演示案件可复跑且账本校验通过。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.demo import run_demo
from src.ledger import InMemoryLedger
from src.reports import (
    rectification_report,
    reproduction_report,
    scope_statement,
)
from src.service import CaseService


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = CaseService(InMemoryLedger())
        cls.case_id = run_demo(cls.service)

    def test_scope_report_explains_range_and_gaps(self):
        data = scope_statement(self.service, self.case_id)
        self.assertTrue(data["locked"])
        self.assertEqual(data["scope"]["activity_name"], "冰点焕新节")
        self.assertEqual(len(data["products"]), 2)
        self.assertIn("window", data)
        self.assertIsInstance(data["gaps"], list)
        below = {a["sku"]: a for a in data["price_assessments"]}
        self.assertTrue(below["SKU-FAN-01"]["below_cost"])
        self.assertFalse(below["SKU-POT-02"]["below_cost"])

    def test_reproduction_report_replays_pinned_version(self):
        data = reproduction_report(self.service, self.case_id)
        self.assertTrue(data["decided"])
        integrity = data["integrity"]
        self.assertTrue(integrity["reproducible"])
        # 处罚后下线的规则仍出现在钉住时刻有效规则中，且被列为"处罚后追加"
        self.assertIn("RULE-PLAT-A-COUPON", data["pinned_state"]["rules_active"])
        after = data["appended_after_decision"]["rule_takedowns"]
        self.assertTrue(any(r["rule_id"] == "RULE-PLAT-A-COUPON" for r in after))

    def test_rectification_report_uses_same_window_and_passes(self):
        data = rectification_report(self.service, self.case_id)
        self.assertTrue(data["plan"]["same_window_as_case"])
        self.assertEqual(data["plan"]["skus"], ["SKU-FAN-01"])
        self.assertTrue(data["verification"]["passed"])


class DemoIdempotenceTest(unittest.TestCase):
    def test_demo_runs_on_file_ledger_and_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            service = CaseService(__import__("src.ledger", fromlist=["FileLedger"]).FileLedger(d))
            case_id = run_demo(service)
            service.ledger.verify(case_id)
            path = Path(d) / f"{case_id}.jsonl"
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            # 每行都是合法 JSON 且序号连续
            seqs = [json.loads(line)["seq"] for line in lines]
            self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


class CliSmokeTest(unittest.TestCase):
    def test_cli_demo_list_verify_reports(self):
        with tempfile.TemporaryDirectory() as d:
            def run(*args):
                return subprocess.run(
                    [sys.executable, "-m", "src.cli", *args, "--store", d],
                    capture_output=True, text=True, check=False, cwd=Path(__file__).parents[1],
                )

            r = run("demo")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("处罚版本重现报告", r.stdout)
            self.assertIn("整改核验报告", r.stdout)

            r = run("list")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("demo-2026-001", r.stdout)

            r = run("verify")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("[OK]", r.stdout)

            r = run("scope", "demo-2026-001", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            payload = json.loads(r.stdout)
            self.assertEqual(payload["case_id"], "demo-2026-001")

            r = run("reproduce", "demo-2026-001")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("完整性校验：通过", r.stdout)

            r = run("rectify", "demo-2026-001")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("风险已消除", r.stdout)


if __name__ == "__main__":
    unittest.main()
