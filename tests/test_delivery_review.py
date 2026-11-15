"""短投递v2审阅页与语义绑定测试；不联网、不改变原运行。"""
from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cocoon.delivery_audit import inspect_delivery_run, prepare_delivery_review
from cocoon.reporting import render_delivery_review

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "experiment/delivery_probes/delivery_20261108T090523Z_fb38842073a64ca4ae95b42c082d6f7e"
V2 = ROOT / "experiment/delivery_probes/delivery_20261108T101439Z_99cfd0db64294c3a8c51b1248b03eb98"
SEMANTIC = ROOT / "experiment/review/delivery_v2_semantic.json"


class DeliveryReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit = inspect_delivery_run(V2)
        cls.old = inspect_delivery_run(V1)
        cls.temp = Path(tempfile.mkdtemp(prefix="delivery-review-", dir=ROOT / "tests"))

    def frozen_inspect(self, path):
        return deepcopy(self.audit if Path(path) == V2 else self.old)

    def test_semantic_binds_raw_failed_not_cast(self):
        with patch("cocoon.delivery_audit.inspect_delivery_run", side_effect=self.frozen_inspect):
            result = prepare_delivery_review(V2, SEMANTIC, V1)
        self.assertFalse(result["audit"]["pair_available"])
        self.assertEqual(result["status"], "pair_not_accepted")
        self.assertEqual(result["content_issue_items"], 0)
        self.assertEqual(result["reported_usage"]["total_tokens"], 4034)
        self.assertIs(type(result["audit"]["by_mode"]["plain_restatement"]["parsed_output"]["schema_version"]), float)
        self.assertFalse(result["formal_eligible"])

    def test_semantic_tampering_rejected(self):
        base = json.loads(SEMANTIC.read_bytes())
        for i in range(4):
            item = deepcopy(base)
            if i == 0:
                item["summary_sha256"] = "0" * 64
            elif i == 1:
                item["items"][0]["original_text"] += "被改变"
            elif i == 2:
                item["counts"]["supported"] = 100
            else:
                item["accepted_pair"] = True
            file = self.temp / f"bad_{i}.json"
            file.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
            with patch("cocoon.delivery_audit.inspect_delivery_run", side_effect=self.frozen_inspect):
                with self.assertRaises(ValueError):
                    prepare_delivery_review(V2, file, V1)

    def test_report_contains_current_failure_not_success(self):
        path = self.temp / "review.html"
        with patch("cocoon.delivery_audit.inspect_delivery_run", side_effect=self.frozen_inspect):
            render_delivery_review(path, V2, SEMANTIC, V1)
        text = path.read_text(encoding="utf-8")
        parser = HTMLParser()
        parser.feed(text)
        parser.close()
        self.assertIn("整对尚未通过", text)
        self.assertIn("630", text)
        self.assertIn("614", text)
        self.assertIn("未接纳，仅作诊断", text)
        self.assertNotIn("<script", text)
        self.assertEqual(text.count("<td>"), text.count("</td>"))
        with patch("cocoon.delivery_audit.inspect_delivery_run", side_effect=self.frozen_inspect):
            with self.assertRaises(FileExistsError):
                render_delivery_review(path, V2, SEMANTIC, V1)


if __name__ == "__main__":
    unittest.main()
