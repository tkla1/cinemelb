"""The gate's parsers, .env value check and lock contract.

The scanners themselves are established tools (gitleaks, ruff);
these tests keep the adapter layer from silently dropping their findings.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import security_gate as gate  # noqa: E402


class TestValueCheck(unittest.TestCase):
    def test_known_value_is_found_with_its_offset(self):
        text = "a line\napi=" + "s" * 12 + "\n"
        hits = gate.value_hits(text, [("API_KEY", "s" * 12)])
        self.assertEqual(hits, [("API_KEY", 11)])

    def test_unrelated_text_is_clean(self):
        self.assertEqual(gate.value_hits("the projection starts at 7pm",
                                         [("API_KEY", "s" * 12)]), [])


class TestGitleaksAdapter(unittest.TestCase):
    def test_report_entries_become_findings(self):
        data = [{"RuleID": "aws-access-token", "File": "src/x.py",
                 "StartLine": 4, "Commit": "abcdef1234567890"}]
        findings = gate.gitleaks_findings("secrets/history", data)
        self.assertEqual(len(findings), 1)
        self.assertIn("aws-access-token", findings[0].message)
        self.assertEqual(findings[0].where, "src/x.py:4 @ abcdef1234")

    def test_empty_report_is_clean(self):
        self.assertEqual(gate.gitleaks_findings("secrets/worktree", []), [])


class TestRuffAdapter(unittest.TestCase):
    def test_results_become_findings(self):
        data = [{
            "code": "S501", "message": "request with no certificate",
            "filename": "cinemelb/x.py", "location": {"row": 9, "column": 5},
        }]
        findings = gate.ruff_findings(data)
        self.assertEqual(findings[0].where, "cinemelb/x.py:9")
        self.assertIn("S501", findings[0].message)

    def test_no_results_is_clean(self):
        self.assertEqual(gate.ruff_findings([]), [])


class TestJsSinks(unittest.TestCase):
    def test_eval_is_found(self):
        hits = gate.js_findings("const value = eval(" + "input);\n")
        self.assertTrue(hits)
        self.assertIn("eval", hits[0][0])

    def test_evaluate_is_not_eval(self):
        self.assertEqual(gate.js_findings("page.evaluate(fn)"), [])

    def test_document_write_is_found(self):
        self.assertTrue(gate.js_findings("document.write(" + "html)"))

    def test_plain_code_is_clean(self):
        self.assertEqual(gate.js_findings("const x = JSON.parse(text);"), [])


class TestLock(unittest.TestCase):
    def test_project_lock_names_every_direct_dependency(self):
        findings, detail = gate.check_lock(ROOT)
        self.assertEqual(findings, [])
        self.assertIn("packages", detail)
        self.assertIn("direct", detail)


if __name__ == "__main__":
    unittest.main()
