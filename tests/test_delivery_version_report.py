"""版本无关报告专项测试；只读真实运行，测试产物留在 tests 下，不联网、不清理。"""
import sys
sys.dont_write_bytecode = True

from contextlib import ExitStack
from copy import deepcopy
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cocoon.delivery_audit import inspect_delivery_run, prepare_delivery_review
from cocoon.reporting import render_delivery_version_report
from cocoon.runtime import verify_events

V3 = ROOT / "experiment/delivery_probes/delivery_20261108T105552Z_7fe7dae9d342459885c811b169a74ff1"
SEMANTIC = ROOT / "experiment/review/delivery_v3_semantic.json"


class ReportHTML(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags, self.attributes = [], []
        self.feed(document)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)


class DeliveryVersionReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / "tests").is_dir():
            raise AssertionError("测试父目录必须已存在")
        cls.temp = Path(tempfile.mkdtemp(prefix="delivery_version_report_", dir=ROOT / "tests")).resolve()
        cls.guards = ExitStack()
        cls.addClassCleanup(cls.guards.close)
        for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                       "socket.getaddrinfo", "urllib.request.urlopen", "urllib.request.OpenerDirector.open",
                       "cocoon.delivery.run_delivery", "cocoon.delivery.MiMoTransport.complete",
                       "cocoon.delivery.load_key", "cocoon.delivery._ledger"):
            cls.guards.enter_context(patch(target, side_effect=AssertionError("专项测试禁止联网、调用模型或读取凭据账本")))
        cls.summary = json.loads((V3 / "summary.json").read_bytes())
        protected_roots = [V3, Path(cls.summary["parent_source_path"]),
                           Path(cls.summary["packet_input"]["manifest_source_path"]).parent]
        cls.protected = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                         for root in protected_roots for p in root.rglob("*") if p.is_file()}
        for p in [Path(cls.summary["policy_input"]["source_path"]),
                  *list((ROOT / "experiment/review").rglob("*.html"))]:
            cls.protected[p] = hashlib.sha256(p.read_bytes()).hexdigest()
        cls.audit = inspect_delivery_run(V3)
        cls.review = {"audit": cls.audit, "status": "pair_not_accepted", "formal_eligible": False,
                      "human_reviews_completed": 0, "new_model_calls": 0}
        # 只检查一次依赖；尚无 v3 审阅时，用真实原句构造全为待核查的测试夹具。
        cls.semantic_is_fixture = not SEMANTIC.is_file()
        if cls.semantic_is_fixture:
            items = [{"mode": mode, "index": index, "original_text": sentence["text"],
                      "citations": sentence["citations"], "kind": sentence["kind"], "verdict": "needs_review",
                      "reason": "测试夹具：仅验证原句绑定和页面展示，没有作出语义通过判断。"}
                     for mode, row in cls.audit["by_mode"].items()
                     for index, sentence in enumerate(row["parsed_output"]["sentences"])]
            semantic = {"review_type": "independent_ai_not_human", "run_id": cls.audit["run_id"],
                        "summary_sha256": cls.audit["summary_sha256"],
                        "packet_sha256": cls.audit["source_inputs"]["packet_input"]["canonical_sha256"],
                        "raw_response_sha256_by_mode": {mode: row["response"]["sha256"]
                                                        for mode, row in cls.summary["additions"].items()},
                        "human_reviews_completed": 0, "accepted_pair": cls.audit["pair_available"],
                        "additional_model_calls": 0, "status": "content_issues_found", "items": items,
                        "counts": {"reviewed_items": len(items), "supported": 0, "acceptable_question": 0,
                                   "overstated": 0, "unsupported": 0, "needs_review": len(items),
                                   "content_issue_items": len(items)},
                        "limits": ["测试夹具，仅供渲染和取材绑定测试；不是实际完成的 AI 或人工审阅。"]}
            semantic_path = cls.temp / "semantic_fixture.json"
            with semantic_path.open("x", encoding="utf-8") as stream:
                json.dump(semantic, stream, ensure_ascii=False, indent=2)
        else:
            semantic_path = SEMANTIC
            cls.protected[SEMANTIC] = hashlib.sha256(SEMANTIC.read_bytes()).hexdigest()
        cls.prepared = prepare_delivery_review(V3, semantic_path)

    @classmethod
    def tearDownClass(cls):
        for path, digest in cls.protected.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise AssertionError("原件或旧报告被修改：" + str(path))
        print("\n专项测试产物（保留）：" + str(cls.temp))
        print("语义审阅来源：" + ("真实原句待核查测试夹具" if cls.semantic_is_fixture else str(SEMANTIC)))

    def render(self, name, review=None, tests=None):
        path = self.temp / name
        source = self.review if review is None else review
        original = deepcopy(source)
        with patch("cocoon.reporting.verify_events", wraps=verify_events) as verifier:
            result = render_delivery_version_report(path, source, tests)
        verifier.assert_called_once_with(V3.resolve())
        self.assertEqual(source, original)
        self.assertEqual(result, path)
        document = path.read_text(encoding="utf-8")
        parsed = ReportHTML(document)
        self.assertFalse({"script", "iframe", "img", "link", "object"}.intersection(parsed.tags))
        self.assertFalse(any(key in ("src", "href") or key.startswith("on") for key, _ in parsed.attributes))
        self.assertEqual(document.count("<td>"), document.count("</td>"))
        return document

    def test_01_real_v3_failure_lengths_types_and_missing_review(self):
        document = self.render("real_v3_without_semantic.html")
        self.assertIn(self.audit["policy_version"], document)
        self.assertIn(self.audit["error"]["code"], document)
        self.assertIn(self.audit["failed_stage"], document)
        self.assertIn("整对尚未通过", document)
        self.assertIn("未接纳，仅作诊断", document)
        self.assertIn("仅构建候选，未发送给 Subject", document)
        self.assertIn("—（未提供逐句意见）", document)
        self.assertIn("<td>已提供逐句意见</td><td>—</td>", document)
        self.assertIn("<td>执行数</td><td>—</td>", document)
        self.assertNotIn("8句复核：7句", document)
        self.assertNotIn("schema_version是数字1.0", document)
        self.assertIn("schema_version_type 诊断条数：0", document)
        for mode, row in self.audit["by_mode"].items():
            self.assertIs(type(row["parsed_output"]["schema_version"]), str)
            for size in (row["diagnostic_additional_chars"], row["diagnostic_visible_chars"],
                         self.audit["fixed_chars"], row["range"]["min"], row["range"]["max"]):
                self.assertIn(f"<td>{size}</td>", document)
            self.assertIn(self.summary["additions"][mode]["stage"], document)
            for sentence in row["sentence_lengths"]:
                self.assertIn(f'<td>{sentence["text_chars"]}</td>', document)
        self.assertEqual(document.count("<td>str</td><td>正确</td>"), len(self.audit["by_mode"]))
        for key in ("diagnostic_additional_chars", "diagnostic_visible_chars"):
            values = [row[key] for row in self.audit["by_mode"].values()]
            delta, low = max(values) - min(values), min(values)
            self.assertIn(f"{delta} / {low} ≈ {100 * delta / low:.2f}%", document)
        for field in ("formal_eligible", "subject_delivered"):
            self.assertIn(f"<td>{field}</td><td>false</td>", document)
        self.assertIn("<td>full_I4_eligible（完整 I4）</td><td>false</td>", document)
        self.assertIn("<td>human_reviews_completed（人工审核）</td><td>0</td>", document)
        self.assertIn(f'<td>已验证阶段数</td><td>{self.audit["stage_count"]}</td>', document)
        self.assertIn(f'{self.audit["verification"]["event_count"]} 项事件 / {self.audit["verification"]["blob_count"]} 个 blob', document)

    def test_02_prepare_review_opinions_usage_and_scoped_test_counts(self):
        stats = {"tests_run": 5, "failures": 1, "errors": 0, "skipped": 1, "success": False}
        document = self.render("bound_semantic_review.html", self.prepared, stats)
        items = self.prepared["semantic_review"]["items"]
        self.assertIn(f"<td>已提供逐句意见</td><td>{len(items)}</td>", document)
        for item in items:
            self.assertIn(html.escape(item["reason"], quote=True), document)
            self.assertIn(html.escape(item["original_text"], quote=True), document)
        if self.semantic_is_fixture:
            self.assertIn("测试夹具", document)
            self.assertIn("<td>证据支持</td><td>0</td>", document)
        self.assertIn(f'<td>本轮记账（CNY）</td><td>{self.audit["cost"]["total_cost_cny"]}</td>', document)
        self.assertIn(f'<td>本轮合计 tokens</td><td>{self.prepared["reported_usage"]["total_tokens"]}</td>', document)
        self.assertIn("<td>执行数</td><td>5</td>", document)
        self.assertIn("<td>失败</td><td>1</td>", document)
        self.assertIn("<td>success</td><td>false</td>", document)
        self.assertNotIn("项全项目测试", document)
        self.assertNotIn("次侧诊断614", document)
        self.assertNotIn("保留本次类型失败", document)

    def test_03_mismatched_digests_identity_and_approval_are_rejected(self):
        cases = [("audit", "summary_sha256", "0" * 64),
                 ("audit", "events_sha256", "0" * 64),
                 ("audit", "run_id", "wrong-run"),
                 ("semantic_review", "summary_sha256", "0" * 64),
                 ("semantic_review", "run_id", "wrong-run"),
                 ("semantic_review", "human_reviews_completed", 1),
                 ("audit", "formal_eligible", True),
                 ("audit", "full_I4_eligible", True),
                 ("audit", "subject_delivered", True),
                 (None, "human_reviews_completed", False),
                 (None, "formal_eligible", True)]
        for index, (section, key, value) in enumerate(cases):
            with self.subTest(section=section, key=key):
                review = deepcopy(self.prepared)
                (review[section] if section else review)[key] = value
                path = self.temp / f"rejected_{index}.html"
                with self.assertRaises(ValueError):
                    render_delivery_version_report(path, review)
                self.assertFalse(path.exists())
        path = self.temp / "rejected_events.html"
        with patch("cocoon.reporting.verify_events", side_effect=ValueError("测试事件链不匹配")) as verifier:
            with self.assertRaisesRegex(ValueError, "事件链不匹配"):
                render_delivery_version_report(path, self.review)
            verifier.assert_called_once_with(V3.resolve())
        self.assertFalse(path.exists())

    def test_04_script_content_and_short_references_are_only_escaped_data(self):
        review = deepcopy(self.prepared)
        attack = '<script>alert("测试")</script><img src=x onerror="alert(1)">&\''
        row = review["audit"]["by_mode"]["plain_restatement"]
        row["raw_text"] = attack
        row["diagnostic_additional_text"] = attack
        row["parsed_output"]["schema_version"] = attack
        review["semantic_review"]["items"][0]["reason"] = attack
        review["audit"]["source_map"] = [{"source_url": "javascript:" + attack, "publisher_short": attack}]
        review["audit"]["source_inputs"]["source_index_input"]["source_path"] = attack
        document = self.render("escaped_data_fixture.html", review)
        self.assertIn(html.escape(attack, quote=True), document)
        self.assertNotIn(attack, document)
        self.assertIn("短引用来源映射（仅数据，不访问链接）", document)

    def test_05_none_missing_fields_and_empty_semantic_do_not_invent_success(self):
        review = deepcopy(self.review)
        review["semantic_review"] = {"review_type": "independent_ai_not_human", "run_id": self.audit["run_id"],
                                     "summary_sha256": self.audit["summary_sha256"], "human_reviews_completed": 0,
                                     "items": [], "status": "pass_content_with_structure_blocker",
                                     "counts": {"reviewed_items": 999, "supported": 999}}
        for row in review["audit"]["by_mode"].values():
            row.update(range={"min": None, "max": None}, budget=None, sentence_lengths=None, violations=None,
                       diagnostic_additional_chars=None, diagnostic_visible_chars=None, usage=None,
                       parsed_output=None, raw_text=None, diagnostic_additional_text=None, accepted=None, status=None)
        review["audit"].update(stage_count=None, policy_version=None, cost=None, budget_after=None,
                               source_inputs=None, source_map=None, evidence_block=None)
        review["reported_usage"] = None
        document = self.render("missing_fields_fixture.html", review, {"tests_run": None})
        for title in ("已验证阶段数", "本轮记账（CNY）", "本轮合计 tokens", "policy version", "执行数"):
            self.assertIn(f"<td>{title}</td><td>—</td>", document)
        self.assertIn("<td>已提供逐句意见</td><td>0</td>", document)
        self.assertIn("<td>证据支持</td><td>—</td>", document)
        self.assertIn("语义审阅状态：—", document)
        self.assertNotIn("pass_content_with_structure_blocker", document)
        self.assertNotIn("<td>999</td>", document)
        self.assertNotIn("≈", document)
        self.assertNotIn(">None<", document)
        self.assertIn("schema_version_type 诊断条数：—", document)
        self.assertIn("—（未提供逐句意见）", document)

    def test_06_no_overwrite_and_no_parent_creation(self):
        path = self.temp / "no_overwrite.html"
        self.render(path.name)
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            render_delivery_version_report(path, self.review)
        self.assertEqual(path.read_bytes(), original)
        missing = self.temp / "must_not_create" / "report.html"
        with self.assertRaises(FileNotFoundError):
            render_delivery_version_report(missing, self.review)
        self.assertFalse(missing.parent.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
