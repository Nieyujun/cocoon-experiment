"""探索性摘录备选的报告标签与输入完整性测试。"""
import json
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cocoon.reporting import render_extractive_review

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiment/review/delivery_v3_20261108"
RUN = ROOT / "experiment/delivery_probes/delivery_20261108T105552Z_7fe7dae9d342459885c811b169a74ff1"
POLICY = ROOT / "configs/extractive_control_v1.json"
HASH = "b96e2a12d9b85189e6765c0aa23b04a6f2ad2bec6d57a8d73194d34c2fe5413a"


class ExtractiveReportTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="extractive-report-", dir=ROOT / "tests"))

    def render(self, path, semantic=OUT / "extractive_review.json"):
        return render_extractive_review(path, OUT / "extractive_alternative.json", semantic, RUN, POLICY, HASH)

    def test_report_keeps_original_failure_and_alternative_labels(self):
        target = self.render(self.root / "review.html")
        text = target.read_text(encoding="utf-8")
        parser = HTMLParser()
        parser.feed(text)
        parser.close()
        for term in ("632", "637", "175", "180", "2.86%", "原记录保留，未接纳", "新的离线探索性实现",
                     "deterministic_exact_source_sentences", "formal_eligible=false", "人工审核为0", "组织侧4句、普通侧3句"):
            self.assertIn(term, text)
        self.assertNotIn("<script", text)
        self.assertEqual(text.count("<td>"), text.count("</td>"))
        with self.assertRaises(FileExistsError):
            self.render(target)

    def test_semantic_digest_tamper_rejected(self):
        data = json.loads((OUT / "extractive_review.json").read_bytes())
        data["alternative_file_sha256"] = "0" * 64
        file = self.root / "bad.json"
        file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.render(self.root / "bad.html", file)

    def test_no_network_or_secret_read(self):
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")) as net, \
             patch("cocoon.mimo.load_key", side_effect=AssertionError("credential forbidden")) as key:
            self.render(self.root / "offline.html")
        net.assert_not_called()
        key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
