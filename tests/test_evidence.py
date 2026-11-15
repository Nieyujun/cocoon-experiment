"""候选证据包离线回归；临时测试产物保留，不修改真实来源。"""
from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from cocoon.contracts import validate_packet
from cocoon.evidence import (collect_materials, public_packet, render_evidence_block,
                             verify_bundle, _inside, _quote_count)
from cocoon.runtime import make_intervention_pair
from cocoon.reporting import render_evidence_report

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "experiment/evidence_candidates/v1_20261108"
BUILD = SOURCE / "build_20261108T074108_f38ec5"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.script = ""
        self.in_script = False
        self.card_count = 0
        self.ids = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self.in_script = True
        if a.get("class") == "evidence-card":
            self.card_count += 1
        if "id" in a:
            self.ids.append(a["id"])
    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
    def handle_data(self, data):
        if self.in_script:
            self.script += data


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.materials, cls.sources, cls.source_checks, cls.evidence_checks = collect_materials(SOURCE)

    def test_all_sources_and_quotes_real_local_match(self):
        self.assertEqual(len(self.materials), 3)
        self.assertEqual(len(self.sources), 13)
        self.assertEqual(len(self.evidence_checks), 18)
        self.assertTrue(all(row["quote_exact_match"] for row in self.evidence_checks))
        self.assertTrue(all(row["http_status"] == 200 for row in self.source_checks))

    def test_export_fixed_allowlist_no_review_leak(self):
        for material in self.materials:
            p = public_packet(material)
            self.assertEqual(len(p["evidence"]), 6)
            self.assertFalse(p["is_demo"])
            validate_packet(p, allow_demo=False)
            for row in p["evidence"]:
                self.assertEqual(set(row), {"evidence_id", "text", "source_url", "source_family_id", "content_sha256"})
                self.assertNotIn("review_dimension", row)
                self.assertNotIn("boundary_note", row)
                self.assertNotIn("original_quote", row)

    def test_evidence_copy_isolated(self):
        original = deepcopy(self.materials[0])
        packet = public_packet(self.materials[0])
        packet["evidence"][0]["text"] = "changed only the copy"
        self.assertEqual(self.materials[0], original)

    def test_dose_render_matches_runtime_without_dummy_delivery(self):
        p = public_packet(self.materials[0], self.materials[0]["evidence"][:3], "R1")
        compatible = {"packet_id": p["packet_id"], "evidence": [{key: value for key, value in item.items()
                      if key != "content_sha256"} for item in p["evidence"]]}
        pair = make_intervention_pair(compatible, "仅测试", "仅测试", min_chars=0, max_chars=100000)
        self.assertEqual(render_evidence_block(p), pair["I4"]["evidence_block"])

    def test_bundle_check_and_human_status(self):
        result = verify_bundle(BUILD)
        self.assertTrue(result["passed"])
        self.assertEqual(result["packet_count"], 9)
        self.assertEqual(result["evidence_count"], 18)
        self.assertFalse(result["approved_for_experiment"])
        self.assertEqual(result["human_reviews_completed"], 0)

    def test_tampered_copy_rejected(self):
        root = Path(tempfile.mkdtemp(prefix="evidence-", dir=ROOT / "tests"))
        manifest = json.loads((BUILD / "manifest.json").read_text(encoding="utf-8"))
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        for row in manifest["files"]:
            target = root / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((BUILD / row["path"]).read_bytes())
        target = root / manifest["topics"][0]["full_packet"]
        target.write_bytes(target.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            verify_bundle(root)

    def test_paths_and_quote_budget(self):
        with self.assertRaises(ValueError):
            _inside(SOURCE, SOURCE / "../elsewhere.json")
        self.assertEqual(_quote_count("Two words.", "en"), 2)
        for source in self.sources.values():
            self.assertLessEqual(source["direct_quote_units"], source["direct_quote_limit"])

    def test_history_date_in_public_weather_text(self):
        material = self.materials[2]
        for entry in material["evidence"]:
            if entry["evidence_id"] in ("WE_E04", "WE_E05"):
                self.assertIn("二〇二〇年五月十二日", entry["text"])

    def test_report_structure_no_double_ids_and_js_syntax(self):
        root = Path(tempfile.mkdtemp(prefix="evidence-report-", dir=ROOT / "tests"))
        path = render_evidence_report(root / "review.html", BUILD)
        text = path.read_text(encoding="utf-8")
        parser = PageParser()
        parser.feed(text)
        self.assertEqual(parser.card_count, 18)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertIn("双人人工审核为0", text)
        self.assertIn("835", text)
        self.assertNotIn("<iframe", text)
        node = shutil.which("node")
        if node:
            run = subprocess.run([node, "--check"], input=parser.script, encoding="utf-8", text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
        with self.assertRaises(FileExistsError):
            render_evidence_report(path, BUILD)


if __name__ == "__main__":
    unittest.main()
