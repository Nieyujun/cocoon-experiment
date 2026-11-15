"""已保存真实结果的离线审阅绑定和HTML校验，不产生收费调用。"""
from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from cocoon.dose_build import source_index_from_review
from cocoon.live_audit import prepare_live_review
from cocoon.reporting import render_live_review

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "experiment/mimo_probes/assistant_probe_20261108T082120_9ad78cf8"
EVIDENCE = ROOT / "experiment/evidence_candidates/v1_20261108/build_20261108T074108_f38ec5"
SEMANTIC = ROOT / "experiment/review/live_semantic_20261108.json"


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inscript = False
        self.script = ""
        self.cards = 0
        self.problems = 0
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self.inscript = True
        if tag == "article":
            self.cards += 1
            self.problems += a.get("data-problem") == "yes"
    def handle_endtag(self, tag):
        if tag == "script":
            self.inscript = False
    def handle_data(self, text):
        if self.inscript:
            self.script += text


class LiveReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "configs/core.json").read_bytes())
        cls.index = source_index_from_review(EVIDENCE)
        cls.review = cls.prepare(SEMANTIC)

    @classmethod
    def prepare(cls, semantic):
        return prepare_live_review(RUN, EVIDENCE / "packets/TOPIC_01_round_1.json",
                ROOT / "configs/assistant_prompts_v1.json", cls.config["assistant"]["prompt_bundle_sha256"],
                cls.index, semantic, ROOT / "experiment/billing/mimo_initial_probe_v1.jsonl")

    def test_real_billing_and_problem_counts(self):
        review = self.review
        self.assertEqual(review["problem_items"], 11)
        self.assertEqual(review["semantic_review"]["items_reviewed"], 39)
        self.assertEqual(review["billing"]["run_cost_cny"], "0.022503")
        self.assertTrue(review["billing"]["run_all_attempts_settled"])
        self.assertEqual(review["audit"]["candidate_visible_chars"], 2051)
        self.assertEqual(review["audit"]["report_chars"], 1594)
        self.assertFalse(review["formal_eligible"])
        self.assertFalse(review["original_run_modified"])

    def test_modified_semantic_claims_rejected(self):
        root = Path(tempfile.mkdtemp(prefix="live-review-", dir=ROOT / "tests"))
        original = json.loads(SEMANTIC.read_bytes())
        mutations = [
            lambda d: d.update(summary_sha256="0" * 64),
            lambda d: d.update(human_reviews_completed=2),
            lambda d: d["items"].pop(),
            lambda d: d["items"].append(deepcopy(d["items"][0])),
            lambda d: d["items"][0].update(text="这不是原始模型回复"),
            lambda d: d["counts"].update(problem_items=0),
            lambda d: d.update(status="pass_with_limitations"),
        ]
        for index, change in enumerate(mutations):
            with self.subTest(case=index):
                data = deepcopy(original)
                change(data)
                path = root / f"semantic_{index}.json"
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.prepare(path)

    def test_html_originals_flags_and_script(self):
        root = Path(tempfile.mkdtemp(prefix="live-review-html-", dir=ROOT / "tests"))
        path = render_live_review(root / "review.html", self.review)
        text = path.read_text(encoding="utf-8")
        parser = Page()
        parser.feed(text)
        self.assertEqual(parser.cards, 39)
        self.assertEqual(parser.problems, 11)
        self.assertIn("2051", text)
        self.assertIn("未截断到800", text)
        self.assertIn("缺乏支持的事实", text)
        self.assertNotIn("<iframe", text)
        self.assertNotIn("src=", text)
        node = shutil.which("node")
        if node:
            result = subprocess.run([node, "--check"], input=parser.script, text=True,
                                    capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(FileExistsError):
            render_live_review(path, self.review)


if __name__ == "__main__":
    unittest.main()
