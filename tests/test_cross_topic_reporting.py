"""只跑本文件：真实源重验，HTML写入内存，不生成页面或修改审核记录。"""

import sys
sys.dont_write_bytecode = True

from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import html
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import socket
import unittest
from unittest import mock
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cocoon import delivery, extractive_control, human_review
from cocoon.reporting import render_cross_topic_review

REVIEW = ROOT / "experiment/review/cross_topic_20261108"
FEASIBILITY = REVIEW / "feasibility.json"
CHECKLIST = REVIEW / "human_review_checklist.json"
INDEPENDENT = REVIEW / "independent_review.json"
OUTPUT = REVIEW / "__cross_topic_reporting_test_only__.html"
BUILD = ROOT / "experiment/evidence_candidates/v1_20261108/build_20261108T074108_f38ec5"
POLICY = ROOT / "configs/extractive_control_v1.json"
POLICY_SHA = "b96e2a12d9b85189e6765c0aa23b04a6f2ad2bec6d57a8d73194d34c2fe5413a"
RUN = ROOT / "experiment/delivery_probes/delivery_20261108T105552Z_7fe7dae9d342459885c811b169a74ff1"
ALTERNATIVE = ROOT / "experiment/review/delivery_v3_20261108/extractive_alternative.json"
SEMANTIC = ROOT / "experiment/review/live_semantic_20261108.json"
CALL_ARGS = dict(evidence_build=BUILD.parent, policy_path=POLICY, expected_policy_sha=POLICY_SHA,
                 recorded_alternative_path=ALTERNATIVE, recorded_run_directory=RUN,
                 live_semantic_path=SEMANTIC)
GROUPS = ("evidence_reviews", "source_reviews", "model_issue_reviews", "round_reviews", "release_gates")
REPORTING_BASE_BYTES = 116782
REPORTING_BASE_SHA = "734bf8ed92eb22f130d5bb2c03aa2f6f4b990b575f9177c22a9d21a5e2d8fd8d"


def sha(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


class PageParser(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.elements = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


@contextmanager
def memory_page(overlays=None):
    """只替换输入字节和HTML输出介质；正常用例不替换任何业务证明。"""
    overlays = {p.resolve(): data for p, data in (overlays or {}).items()}
    original_read, original_open = Path.read_bytes, io.open
    captured = {"page": "", "modes": []}

    class Sink(io.StringIO):
        def close(self):
            captured["page"] = self.getvalue()
            super().close()

    def read_bytes(path):
        return overlays[path.resolve()] if path.resolve() in overlays else original_read(path)

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            if Path(file).resolve() != OUTPUT or mode != "x":
                raise AssertionError("只允许以独占新建模式输出测试HTML到内存")
            captured["modes"].append(mode)
            return Sink()
        return original_open(file, mode, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "read_bytes", read_bytes))
        stack.enter_context(mock.patch("io.open", guarded_open))
        stack.enter_context(mock.patch("builtins.open", guarded_open))
        guards = [stack.enter_context(mock.patch.object(socket, "socket", side_effect=AssertionError("禁止联网")))]
        for name in ("complete", "_exchange"):
            guards.append(stack.enter_context(mock.patch.object(
                delivery.MiMoTransport, name, side_effect=AssertionError("禁止模型请求"))))
        for name in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open"):
            guards.append(stack.enter_context(mock.patch(name, side_effect=AssertionError("禁止获取网页"))))
        try:
            yield captured
        finally:
            for guard in guards:
                guard.assert_not_called()


class CrossTopicReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not REVIEW.is_dir() or OUTPUT.exists():
            raise AssertionError("真实输入目录必须存在，测试目标不得已有文件")
        cls.feasibility_bytes, cls.checklist_bytes = FEASIBILITY.read_bytes(), CHECKLIST.read_bytes()
        cls.feasibility, cls.checklist = json.loads(cls.feasibility_bytes), json.loads(cls.checklist_bytes)
        cls.independent_bytes = INDEPENDENT.read_bytes()
        cls.independent = json.loads(cls.independent_bytes)
        protected = {FEASIBILITY, CHECKLIST, INDEPENDENT, POLICY, ALTERNATIVE, SEMANTIC, ROOT / "cocoon/reporting.py"}
        protected.update(Path(name) for name in cls.feasibility["input_hashes"]["files"])
        protected.update(Path(name) for name in cls.checklist["source_inputs"])
        protected.update((ROOT / "cocoon").glob("*.py"))
        cls.before = {path: sha(path.read_bytes()) for path in protected}
        cls.review_files = set(REVIEW.iterdir())
        with memory_page() as captured, mock.patch.object(
            extractive_control, "verify_extractive_feasibility", wraps=extractive_control.verify_extractive_feasibility
        ) as verify, mock.patch.object(
            human_review, "validate_human_review_template", wraps=human_review.validate_human_review_template
        ) as validate:
            cls.returned = render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST, **CALL_ARGS)
            cls.verify_calls, cls.validate_calls = verify.call_args_list, validate.call_args_list
        cls.page, cls.output_modes = captured["page"], captured["modes"]

    @classmethod
    def tearDownClass(cls):
        if {path: sha(path.read_bytes()) for path in cls.before} != cls.before:
            raise AssertionError("原始输入或旧代码发生变化")
        if set(REVIEW.iterdir()) != cls.review_files or OUTPUT.exists():
            raise AssertionError("不应生成正式HTML、人工副本或其他输出文件")

    def test_01_real_inputs_static_summary_cards_and_print(self):
        page = self.page
        self.assertEqual(self.returned, OUTPUT)
        self.assertEqual(self.output_modes, ["x"])
        self.assertEqual(len(self.verify_calls), 1)
        self.assertEqual(self.verify_calls[0].args[1:], (BUILD, POLICY, POLICY_SHA))
        self.assertEqual(self.verify_calls[0].kwargs, dict(recorded_alternative_path=ALTERNATIVE,
                                                        recorded_run_directory=RUN))
        self.assertEqual(len(self.validate_calls), 1)
        self.assertEqual(self.validate_calls[0].args[1:], (BUILD, SEMANTIC))
        self.assertIn("6 有条件可行 / 1 已绑定旧探索性候选 / 5 缺实际组织输出 / 新增请求 0", page)
        self.assertIn("632 / 637（旧探索性候选）", page)
        self.assertIn("18 证据 / 13 来源 / 11 模型问题 / 6 轮次 / 7 门槛", page)
        for text in ("未附AI复核", "本页生成时未附全项目结果", "不是签署系统", "不会保存填写",
                     "原 v3 失败没有被修复", "无真实自然 T1", "人工完成 0", "formal_eligible=false",
                     "不是英文 source 逐字原句", "包含引文标记字符", "Ctrl+P", "各自另存",
                     "原始来源网页和全文不打包", "不计入受试消息"):
            self.assertIn(text, page)
        for text in ("六对成功", "coverage_ratio", "覆盖比例", "localStorage", "fetch("):
            self.assertNotIn(text, page)
        parsed = PageParser(page)
        forbidden = {"script", "input", "textarea", "select", "form", "button", "iframe", "img", "object", "embed", "link"}
        self.assertFalse(forbidden.intersection(tag for tag, _ in parsed.elements))
        for tag, attrs in parsed.elements:
            self.assertFalse(any(name.startswith("on") or name == "contenteditable" for name in attrs))
            if tag == "details":
                self.assertNotIn("open", attrs)
        cards = [attrs for _, attrs in parsed.elements if attrs.get("class") == "review-card"]
        self.assertEqual(len(cards), 55)
        self.assertEqual({attrs["id"] for attrs in cards}, {item["id"] for group in GROUPS for item in self.checklist[group]})
        self.assertEqual(page.count('class="reviewer"'), 110)
        self.assertEqual(page.count('class="adjudication"'), 55)
        self.assertEqual(page.count("初步AI意见，非人工裁决"), 11)
        self.assertIn("@media print", page)
        self.assertIn("details>*,details:not([open])>*{display:block!important}", page)
        self.assertIn("details::details-content{display:block!important;content-visibility:visible!important", page)
        self.assertIn("grid-template-columns:1fr 1fr", page)
        self.assertIn("min-height:10mm", page)
        downloads = [attrs for tag, attrs in parsed.elements if tag == "a" and "download" in attrs]
        self.assertEqual([attrs["href"] for attrs in downloads], ["./feasibility.json", "./human_review_checklist.json"])
        for case in self.feasibility["cases"]:
            self.assertIn(f'<article id="{case["case_id"]}">', page)
            for pool in case["sentence_pools"]:
                for sentence in pool["sentences"]:
                    self.assertIn(html.escape(sentence["text"]), page)
                    self.assertIn(f'offset [{sentence["start"]}, {sentence["end"]})', page)
            for combo in case["candidate_combinations"]:
                self.assertIn(f'<td>{combo["plain_additional_chars"]}</td><td>{combo["plain_visible_chars"]}</td>', page)
        for group, fields in (("evidence_reviews", ("text", "original_quote", "boundary_note")),
                              ("source_reviews", ("source_level", "source_family_id", "license_note")),
                              ("model_issue_reviews", ("text", "reason")), ("release_gates", ("required_action",))):
            for item in self.checklist[group]:
                for field in fields:
                    self.assertIn(html.escape(item[field]), page)

    def test_02_forged_summary_and_other_actual_round_rejected(self):
        summary = deepcopy(self.feasibility)
        summary["summary"]["actual_pair_available_cases"] = 6
        other = deepcopy(self.feasibility)
        other["cases"][1].update({key: value for key, value in other["cases"][0].items() if key.startswith("actual_")})
        other["cases"][1]["status"] = "recorded_exploratory_candidate"
        other["summary"]["actual_pair_available_cases"] = 2
        for name, forged in (("摘要", summary), ("其他轮次实际输出", other)):
            with self.subTest(name=name), memory_page({FEASIBILITY: encoded(forged)}) as captured:
                with self.assertRaisesRegex(ValueError, "feasibility_result_mismatch"):
                    render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST, **CALL_ARGS)
                self.assertEqual(captured["modes"], [])

    def test_03_signed_or_approved_template_rejected(self):
        forged = deepcopy(self.checklist)
        for key in ("reviewer_1", "reviewer_2"):
            forged["evidence_reviews"][0][key].update(reviewer_id="同一伪造审核者", decision="approved")
        forged.update(approved_for_experiment=True, human_reviews_completed=2, formal_eligible=True)
        with memory_page({CHECKLIST: encoded(forged)}) as captured, mock.patch.object(
            human_review, "validate_human_review_template", wraps=human_review.validate_human_review_template
        ) as validate:
            with self.assertRaisesRegex(ValueError, "human_template_invalid"):
                render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST, **CALL_ARGS)
            validate.assert_called_once()
            self.assertEqual(captured["modes"], [])

    def test_04_ui_escaping_only_mocked_proof_and_actual_download_names(self):
        malicious = '<script>alert("审核")</script><img src=x onerror=alert(1)>&\''
        feasibility, checklist = deepcopy(self.feasibility), deepcopy(self.checklist)
        feasibility["cases"][0]["title"] = malicious
        feasibility["cases"][0]["sentence_pools"][0]["sentences"][0]["text"] = malicious
        for group, field in (("evidence_reviews", "boundary_note"), ("source_reviews", "license_note"),
                             ("model_issue_reviews", "reason"), ("release_gates", "required_action")):
            checklist[group][0][field] = malicious
        safe_url = checklist["source_reviews"][0]["url"] + "?review=A&case=B"
        unsafe_urls = ["javascript:alert(1)", "data:text/html,unsafe", "//invalid/path", "http://invalid/path",
                       "https://name:password@invalid/path", 'https://invalid/" onmouseover="bad',
                       "https://invalid\\path", "https://invalid/\npath", "https://[invalid"]
        for item, url in zip(checklist["source_reviews"], unsafe_urls + [safe_url]):
            item["url"] = url
        alternate_f = REVIEW / "可行性 #&.json"
        alternate_h = REVIEW / "人审 待填.json"
        test_count = unittest.defaultTestLoader.loadTestsFromTestCase(type(self)).countTestCases()
        with memory_page({alternate_f: encoded(feasibility), alternate_h: encoded(checklist)}) as captured:
            with mock.patch.object(extractive_control, "verify_extractive_feasibility", return_value=True) as verify:
                with mock.patch.object(human_review, "validate_human_review_template", return_value=True) as validate:
                    render_cross_topic_review(OUTPUT, alternate_f, alternate_h,
                                             tests=dict(tests_run=test_count, failures=0, errors=0), **CALL_ARGS)
                    verify.assert_called_once()
                    validate.assert_called_once()
        page = captured["page"]
        self.assertIn(html.escape(malicious, quote=True), page)
        self.assertNotIn(malicious, page)
        self.assertIn(html.escape(safe_url, quote=True), page)
        self.assertIn(f"随页提供的测试记录：{test_count} 项", page)
        parsed = PageParser(page)
        self.assertFalse({"script", "img", "input"}.intersection(tag for tag, _ in parsed.elements))
        links = [attrs for tag, attrs in parsed.elements if tag == "a"]
        self.assertFalse(set(unsafe_urls).intersection(attrs["href"] for attrs in links))
        for attrs in links:
            self.assertFalse(any(key.startswith("on") for key in attrs))
            if not attrs["href"].startswith(("#", "./")):
                self.assertEqual(urlsplit(attrs["href"]).scheme, "https")
                self.assertEqual(attrs["rel"], "noopener noreferrer")
        downloads = [attrs for attrs in links if "download" in attrs]
        self.assertEqual([attrs["href"] for attrs in downloads],
                         ["./" + quote(p.name, safe="") for p in (alternate_f, alternate_h)])
        self.assertEqual([unquote(attrs["href"][2:]) for attrs in downloads], [alternate_f.name, alternate_h.name])

    def test_05_independent_binding_unique_manifest_and_no_overwrite(self):
        independent_path = REVIEW / "__independent_review_test_only__.json"
        independent = dict(review_type="independent_ai_not_human",
                           feasibility_file_sha256=sha(self.feasibility_bytes),
                           checklist_file_sha256=sha(self.checklist_bytes),
                           human_reviews_completed=0, formal_eligible=False,
                           status="pass_with_limitations", limits=["测试意见：不代替人工裁决"])
        with memory_page({independent_path: encoded(independent)}) as captured:
            render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST,
                                      independent_review_path=independent_path, **CALL_ARGS)
        self.assertIn("已附独立AI语境意见", captured["page"])
        self.assertIn("formal_eligible=false", captured["page"])
        self.assertIn("人工完成 0", captured["page"])
        for field in ("feasibility_file_sha256", "checklist_file_sha256"):
            forged = {**independent, field: "0" * 64}
            with self.subTest(field=field), memory_page({independent_path: encoded(forged)}):
                with self.assertRaisesRegex(ValueError, "independent_review_binding_mismatch"):
                    render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST,
                                              independent_review_path=independent_path, **CALL_ARGS)
        with memory_page():
            with self.assertRaises(FileNotFoundError):
                render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST,
                                          independent_review_path=independent_path, **CALL_ARGS)
            with self.assertRaises(FileExistsError):
                render_cross_topic_review(FEASIBILITY, FEASIBILITY, CHECKLIST, **CALL_ARGS)
        duplicate = deepcopy(self.feasibility)
        duplicate["input_hashes"]["files"][str(BUILD / "unused/manifest.json")] = duplicate["input_hashes"]["manifest_sha256"]
        with memory_page({FEASIBILITY: encoded(duplicate)}):
            with self.assertRaisesRegex(ValueError, "unique_source_manifest_required"):
                render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST, **CALL_ARGS)
        self.assertEqual({path: sha(path.read_bytes()) for path in self.before}, self.before)
        self.assertEqual(set(REVIEW.iterdir()), self.review_files)
        self.assertFalse(OUTPUT.exists())
        reporting = (ROOT / "cocoon/reporting.py").read_bytes()
        self.assertEqual(sha(reporting[:REPORTING_BASE_BYTES]), REPORTING_BASE_SHA)

    def test_06_real_independent_nested_binding_renders_without_modification(self):
        review = self.independent
        self.assertEqual(review["schema"], "independent_ai_review")
        self.assertEqual(review["review_type"], "independent_ai_not_human")
        self.assertEqual(review["input_sha256"], {
            "feasibility.json": sha(self.feasibility_bytes),
            "human_review_checklist.json": sha(self.checklist_bytes),
        })
        self.assertNotIn("feasibility_file_sha256", review)
        self.assertNotIn("checklist_file_sha256", review)
        with memory_page() as captured:
            render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST,
                                      independent_review_path=INDEPENDENT, **CALL_ARGS)
        page = captured["page"]
        self.assertEqual(captured["modes"], ["x"])
        self.assertIn("已附独立AI语境意见", page)
        self.assertIn(review["status"], page)
        self.assertIn(html.escape(review["status_detail"], quote=True), page)
        for priority in review["human_priorities"]:
            self.assertIn(html.escape(priority["question"], quote=True), page)
        self.assertIn("人工完成 0", page)
        self.assertIn("formal_eligible=false", page)
        self.assertEqual(INDEPENDENT.read_bytes(), self.independent_bytes)
        self.assertEqual(FEASIBILITY.read_bytes(), self.feasibility_bytes)
        self.assertEqual(CHECKLIST.read_bytes(), self.checklist_bytes)

    def test_07_nested_hash_corruption_and_byte_changes_are_rejected(self):
        for field in ("feasibility.json", "human_review_checklist.json"):
            forged = deepcopy(self.independent)
            forged["input_sha256"][field] = "0" * 64
            with self.subTest(field=field), memory_page({INDEPENDENT: encoded(forged)}) as captured:
                with self.assertRaisesRegex(ValueError, "independent_review_binding_mismatch"):
                    render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST,
                                              independent_review_path=INDEPENDENT, **CALL_ARGS)
                self.assertEqual(captured["modes"], [])
        # JSON值不变，但实际字节发生变化；不得改用canonical JSON哈希放行。
        for path, content in ((FEASIBILITY, self.feasibility_bytes), (CHECKLIST, self.checklist_bytes)):
            with self.subTest(path=path.name), memory_page({path: content + b"\n"}) as captured:
                with self.assertRaisesRegex(ValueError, "independent_review_binding_mismatch"):
                    render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST,
                                              independent_review_path=INDEPENDENT, **CALL_ARGS)
                self.assertEqual(captured["modes"], [])

    def test_08_both_binding_formats_must_agree_without_fallback(self):
        both = deepcopy(self.independent)
        both.update(feasibility_file_sha256=sha(self.feasibility_bytes),
                    checklist_file_sha256=sha(self.checklist_bytes))
        with memory_page({INDEPENDENT: encoded(both)}) as captured:
            render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST,
                                      independent_review_path=INDEPENDENT, **CALL_ARGS)
        self.assertIn("已附独立AI语境意见", captured["page"])
        for field in ("feasibility_file_sha256", "checklist_file_sha256",
                      "feasibility.json", "human_review_checklist.json"):
            forged = deepcopy(both)
            if field.endswith(".json"):
                forged["input_sha256"][field] = "0" * 64
            else:
                forged[field] = "0" * 64
            with self.subTest(field=field), memory_page({INDEPENDENT: encoded(forged)}) as captured:
                with self.assertRaisesRegex(ValueError, "independent_review_binding_mismatch"):
                    render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST,
                                              independent_review_path=INDEPENDENT, **CALL_ARGS)
                self.assertEqual(captured["modes"], [])

    def test_09_missing_partial_or_malformed_bindings_are_rejected(self):
        base = {k: v for k, v in self.independent.items() if k != "input_sha256"}
        malformed = [base, {**base, "feasibility_file_sha256": sha(self.feasibility_bytes)}]
        for value in (None, [], "not-a-mapping", {}, {"feasibility.json": sha(self.feasibility_bytes)}):
            malformed.append({**base, "input_sha256": value})
        # 另一种绑定正确也不能掩盖已声明但不完整的格式。
        malformed.append({**deepcopy(self.independent), "feasibility_file_sha256": sha(self.feasibility_bytes)})
        malformed.append({**base, "feasibility_file_sha256": sha(self.feasibility_bytes),
                          "checklist_file_sha256": sha(self.checklist_bytes), "input_sha256": None})
        for index, forged in enumerate(malformed):
            with self.subTest(index=index), memory_page({INDEPENDENT: encoded(forged)}) as captured:
                with self.assertRaisesRegex(ValueError, "independent_review_binding_mismatch"):
                    render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST,
                                              independent_review_path=INDEPENDENT, **CALL_ARGS)
                self.assertEqual(captured["modes"], [])

    def test_10_identity_and_approval_guards_apply_to_both_formats(self):
        flat = {k: v for k, v in self.independent.items() if k != "input_sha256"}
        flat.update(feasibility_file_sha256=sha(self.feasibility_bytes),
                    checklist_file_sha256=sha(self.checklist_bytes))
        invalid = [("review_type", "human_review"), ("review_type", None),
                   ("human_reviews_completed", False), ("human_reviews_completed", 0.0),
                   ("human_reviews_completed", "0"), ("human_reviews_completed", 1),
                   ("formal_eligible", True), ("formal_eligible", 0), ("formal_eligible", None)]
        for format_name, base in (("nested", self.independent), ("flat", flat)):
            for field, value in invalid:
                forged = deepcopy(base)
                forged[field] = value
                with self.subTest(format=format_name, field=field, value=value):
                    with memory_page({INDEPENDENT: encoded(forged)}) as captured:
                        with self.assertRaisesRegex(ValueError, "independent_review_binding_mismatch"):
                            render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST,
                                                      independent_review_path=INDEPENDENT, **CALL_ARGS)
                        self.assertEqual(captured["modes"], [])

    def test_11_independent_file_change_during_render_prevents_output(self):
        reads = 0
        original = Path.read_bytes

        def changing_read(path):
            nonlocal reads
            if path.resolve() == INDEPENDENT:
                reads += 1
                return self.independent_bytes if reads == 1 else self.independent_bytes + b"\n"
            return original(path)

        with memory_page() as captured, mock.patch.object(Path, "read_bytes", changing_read):
            with self.assertRaisesRegex(ValueError, "inputs_changed_during_render"):
                render_cross_topic_review(OUTPUT, FEASIBILITY, CHECKLIST,
                                          independent_review_path=INDEPENDENT, **CALL_ARGS)
            self.assertEqual(captured["modes"], [])
        self.assertEqual(reads, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
