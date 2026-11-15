"""步骤37：三议题全链路可视化看板与离线结案交付包专项测试。

纯内存 + 临时目录；禁止网络 socket；不写入项目内任何审阅/取证目录，
步骤28–36 全部受保护原件字节不变。
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import tempfile
import unittest
from unittest import mock

from cocoon import dashboard as db
from cocoon.runtime import canonical_hash

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "experiment/evidence_candidates/v1_20261108"
BUILD = SOURCE_ROOT / "build_20261108T074108_f38ec5"
CROSS = ROOT / "experiment/review/cross_topic_20261108"
MREVIEW = ROOT / "experiment/review/measurement_20261108"
PILOT = ROOT / "experiment/review/pilot_materials_20261109"
AIREV = ROOT / "experiment/review/ai_assisted_review_20261110"
DASHDIR = ROOT / "experiment/dashboard_20261110"
TEMPLATE = CROSS / "human_review_checklist.json"

PROTECTED = [CROSS / name for name in (
    "feasibility.json", "human_review_checklist.json", "independent_review.json",
    "review.html", "delivery.json", "REPORT_step28_20261108.md", "REPORT_step29_20261108.md")]
PROTECTED += [MREVIEW / name for name in (
    "REPORT_step30_20261108.md", "REPORT_step31_20261108.md", "REPORT_step32_20261108.md")]
PROTECTED += [PILOT / name for name in (
    "REPORT_step33_20261109.md", "REPORT_step34_20261109.md", "REPORT_step35_20261109.md")]
PROTECTED += [AIREV / name for name in (
    "REPORT_step36_20261110.md", "scheme_a_merged_verified.json")]
PROTECTED += [
    ROOT / "experiment/billing/mimo_initial_probe_v1.jsonl",
    ROOT / "破茧助手全量实验方案_v1.md",
    ROOT / "experiment/review/live_semantic_20261108.json",
    ROOT / "experiment/review/validation_20261111T061740_93ade4.json",
    ROOT / "configs/core.json",
    ROOT / "PROGRESS.md",
]
PROTECTED += [ROOT / "cocoon" / name for name in (
    "metrics.py", "runtime.py", "contracts.py", "scheduling.py", "human_review.py",
    "measurement.py", "analysis.py", "pilot_materials.py", "system_rehearsal.py",
    "ai_assisted_review.py", "dashboard.py")]
PROTECTED += [ROOT / "tests" / name for name in (
    "test_pilot_materials.py", "test_measurement.py", "test_system_rehearsal.py",
    "test_ai_assisted_review.py")]
PROTECTED += [BUILD / "manifest.json", BUILD / "packets/TOPIC_01_round_1.json"]


def resign(dataset):
    dataset["dataset_sha256"] = canonical_hash(
        {key: value for key, value in dataset.items() if key != "dataset_sha256"})
    return dataset


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
        cls.template_before = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
        cls.directories = [d for d in (CROSS, MREVIEW, PILOT, AIREV, DASHDIR, BUILD) if d.is_dir()]
        cls.directory_before = {d: set(d.iterdir()) for d in cls.directories}
        cls.tmp = Path(tempfile.mkdtemp(prefix="dashboard_case_"))
        with mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")):
            cls.dataset = db.build_dashboard_dataset(SOURCE_ROOT)
        cls.case_dir = cls.tmp / "primary"
        cls.case_dir.mkdir()
        (cls.case_dir / "dashboard_data.json").write_text(
            json.dumps(cls.dataset, ensure_ascii=False, indent=1, allow_nan=False),
            encoding="utf-8", newline="\n")
        db.render_experiment_dashboard(cls.case_dir / "dashboard.html", cls.dataset)
        cls.verification = db.verify_dashboard_bundle(cls.case_dir)
        cls.html = (cls.case_dir / "dashboard.html").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        if cls.protected_before != {p: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in cls.protected_before}:
            raise AssertionError("步骤28–36受保护原件被改写")
        if cls.template_before != hashlib.sha256(TEMPLATE.read_bytes()).hexdigest():
            raise AssertionError("原空白人审模板被改写")
        if cls.directory_before != {d: set(d.iterdir()) for d in cls.directories}:
            raise AssertionError("测试不得向既有审阅或取证目录写入文件")

    def setUp(self):
        self.d = self.__class__.dataset
        self.v = self.__class__.verification
        self.html = self.__class__.html

    def _write_case(self, name, dataset, *, render=True):
        target = self.__class__.tmp / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "dashboard_data.json").write_text(
            json.dumps(dataset, ensure_ascii=False, indent=1, allow_nan=False),
            encoding="utf-8", newline="\n")
        if render:
            db.render_experiment_dashboard(target / "dashboard.html", dataset)
        return target

    def reject(self, directory, code):
        with self.assertRaises(ValueError) as ctx:
            db.verify_dashboard_bundle(directory)
        self.assertIn("dashboard:" + code, str(ctx.exception))

    # ------------------------------------------------------------ 数据包
    def test_01_contract_banner_and_honest_gates(self):
        self.assertEqual(self.d["contract"], db.DATASET_VERSION)
        self.assertEqual(self.d["banner"]["text"], db.BANNER_TEXT)
        self.assertTrue(self.d["banner"]["persistent"])
        for key, expected in (("is_demo", True), ("actual_model_calls", 0),
                              ("actual_api_calls", 0), ("new_cost_cny", "0"),
                              ("human_reviews_completed", 0),
                              ("approved_for_experiment", False),
                              ("formal_eligible", False),
                              ("formal_launch_enabled", False),
                              ("statistical_outputs_are_synthetic", True),
                              ("real_human_ca_signature_verified", False)):
            self.assertIs(self.d["honest_gates"][key], expected, key)
        self.assertEqual(self.d["honest_gates"]["review_origin"], "ai_assisted_dual_perspective_scheme_a")
        body = {k: v for k, v in self.d.items() if k != "dataset_sha256"}
        self.assertEqual(self.d["dataset_sha256"], canonical_hash(body))

    def test_02_overview_matrix_and_counts(self):
        overview = self.d["overview"]
        self.assertEqual((overview["worlds"], overview["branches"],
                          overview["panel_samples"], overview["network_records"],
                          overview["missing_agents_total"]), (24, 36, 1248, 156, 0))
        self.assertEqual(len(overview["matrix"]), 6)
        for row in overview["matrix"]:
            self.assertEqual(row["envs"] if "envs" in row else row["environments"],
                             ["A", "B", "C", "D"])
            self.assertEqual((row["worlds"], row["branches"]), (4, 6))
            self.assertEqual(row["panel_samples"], 208)
            self.assertEqual(row["network_records"], 26)
        self.assertEqual(self.d["parameters"]["topics"], ["TOPIC_01", "TOPIC_02", "TOPIC_03"])

    def test_03_scheme_a_checklist_state(self):
        checklist = self.d["overview"]["checklist"]
        self.assertEqual(checklist["human_slots_completed"], 110)
        self.assertEqual(checklist["items_total"], 55)
        self.assertEqual(checklist["items_passed"], 55)
        self.assertEqual(checklist["disagreement_count"], 4)
        self.assertEqual(checklist["unresolved_disagreements"], 0)
        self.assertEqual(checklist["human_reviews_completed"], 2)
        self.assertTrue(checklist["content_review_passed"])
        self.assertTrue(checklist["release_gates_passed"])
        self.assertTrue(checklist["all_55_items_resolved"])
        self.assertTrue(checklist["all_55_items_passed"])
        self.assertTrue(checklist["pilot_release_unlocked"])
        self.assertEqual(checklist["review_origin"], "ai_assisted_dual_perspective_scheme_a")

    def test_04_billing_and_failure_history(self):
        billing = self.d["overview"]["billing"]
        # 步骤39 发生真实组织侧调用，结算次数与累计金额随之增长；只断言自洽关系。
        self.assertGreaterEqual(billing["settled_calls"], 10)
        # 步骤41 受控提额后有效上限取最后一次 raise_limit；init 原始授权仍为 5.00。
        self.assertEqual(billing["initial_limit_cny"], "5.00")
        expected_limit = (billing["limit_raises"][-1]["new_limit_cny"] if billing["limit_raises"]
                          else billing["initial_limit_cny"])
        self.assertEqual(billing["authorized_limit_cny"], expected_limit)
        self.assertGreaterEqual(float(billing["total_charged_cny"]), 0.03430324)
        self.assertAlmostEqual(
            float(billing["remaining_cny"]),
            float(billing["authorized_limit_cny"]) - float(billing["total_charged_cny"]), places=8)
        failures = self.d["overview"]["failures"]
        self.assertEqual(len(failures), 3)
        self.assertEqual(sorted(row["error_code"] for row in failures),
                         ["invalid_schema", "length_out_of_range", "length_out_of_range"])
        self.assertTrue(all(row["retained"] for row in failures))
        self.assertTrue(failures[0]["detail"])
        self.assertGreaterEqual(len(self.d["overview"]["milestones"]), 3)

    def test_05_panel_rows_complete_and_traceable(self):
        panel = self.d["panel"]
        self.assertEqual(len(panel), 1248)
        self.assertEqual(len({row["sid"] for row in panel}), 1248)
        self.assertTrue(all(row["ok"] for row in panel))
        for row in panel[:50]:
            self.assertIn(row["tp"], ("T0", "T1", "T2", "T3"))
            self.assertIn(row["env"], ("A", "B", "C", "D"))
            self.assertTrue(row["world"].startswith("W_"))
            self.assertEqual(len(row["msha"]), 64)
        self.assertEqual(len({(r["topic"], r["block"]) for r in panel}), 6)

    def test_06_series_shape(self):
        series = self.d["series"]
        self.assertEqual(len(series), 480)
        branch = [s for s in series if s["arm"] == "I4"]
        env = [s for s in series if s["arm"] is None]
        self.assertEqual(len(branch), 3 * 2 * 8)
        self.assertEqual(len(env), 3 * 2 * 4 * 8)
        for row in branch:
            self.assertEqual([p["tp"] for p in row["points"]], ["T1", "T2", "T3"])
        for row in env:
            self.assertEqual([p["tp"] for p in row["points"]], ["T0", "T1"])

    def test_07_three_case_types_with_trace_keys(self):
        cases = self.d["cases"]["cases"]
        for name in ("improved", "unchanged", "reversed"):
            self.assertIsNotNone(cases[name], name)
            key = cases[name]["trace_key"]
            for field in ("topic_id", "block_id", "world_id", "environment",
                          "arm", "agent_id", "item_id", "card_id"):
                self.assertTrue(key[field], (name, field))
        self.assertLess(cases["improved"]["delta_BS"], 0)
        self.assertGreater(cases["reversed"]["delta_BS"], 0)
        self.assertLessEqual(abs(cases["unchanged"]["delta_BS"]),
                             min(abs(cases["improved"]["delta_BS"]),
                                 abs(cases["reversed"]["delta_BS"])))
        self.assertTrue(self.d["cases"]["notes"])

    def test_08_evidence_and_intervention_scale(self):
        evidence = self.d["evidence"]
        self.assertEqual(len(evidence["sources"]), 13)
        self.assertEqual(len(evidence["items"]), 18)
        self.assertEqual(len(evidence["cards"]), 216 + 108)
        self.assertEqual(len(self.d["interventions"]), 36)
        for item in self.d["interventions"]:
            self.assertGreaterEqual(item["char_count"], 600)
            self.assertLessEqual(item["char_count"], 800)
            self.assertEqual(item["response_opportunities"], 1)
            self.assertFalse(item["contains_treatment_label"])
            self.assertTrue(item["extractive_constraint_satisfied"])
        grouped = {}
        for item in self.d["interventions"]:
            grouped.setdefault((item["topic"], item["round"]), {})[item["arm"]] = item
        for key, arms in grouped.items():
            self.assertEqual(set(arms), set(self.d["parameters"]["arms"]))
            self.assertEqual(arms["I4"]["evidence_block_sha256"],
                             arms["I5"]["evidence_block_sha256"])
            self.assertEqual(arms["I4"]["evidence_ids"], arms["I5"]["evidence_ids"])

    def test_09_network_and_info_flow(self):
        self.assertEqual(len(self.d["network"]), 6)
        for node in self.d["network"]:
            self.assertEqual(len(node["nodes"]), 8)
            self.assertEqual(len(node["edges"]), 32)
            self.assertEqual(node["same_stance_edges"], 16)
            self.assertEqual(node["opposite_stance_edges"], 16)
            self.assertEqual({n["stance"] for n in node["nodes"]}, {1, -1})
        flow = {row["environment"]: row for row in self.d["info_flow"]}
        self.assertEqual(flow["A"]["X"], flow["B"]["X"])
        self.assertEqual(flow["C"]["X"], flow["D"]["X"])
        self.assertGreater(flow["A"]["X"], flow["C"]["X"])
        self.assertGreater(flow["A"]["Hn"], flow["C"]["Hn"])
        self.assertEqual(flow["A"]["error_ratio"], 0)
        self.assertEqual(flow["C"]["error_ratio"], 0)
        self.assertGreater(flow["B"]["error_ratio"], 0)
        self.assertGreater(flow["D"]["error_ratio"], 0)

    def test_10_effects_and_holm(self):
        effects = self.d["effects"]
        self.assertTrue(effects["exposure"]["overall"]["estimable"])
        self.assertEqual(set(effects["exposure"]["overall"]["contrasts"]),
                         {"homogeneous", "misinformation", "interaction"})
        for endpoint in ("T2", "T3"):
            node = effects["intervention"][endpoint]
            self.assertEqual(set(node["comparisons"]), {"I4_vs_I0", "I4_vs_I5"})
            for name, item in node["comparisons"].items():
                self.assertIsNotNone(item["p_raw"])
                self.assertIsNotNone(item["p_adjusted"])
                self.assertGreaterEqual(item["p_adjusted"], item["p_raw"])
                self.assertEqual(item["p_value_status"], "computed_from_actual_allocation")
            from cocoon import metrics
            self.assertEqual(metrics.holm(dict(node["p_raw"])), node["p_adjusted"])

    def test_11_calibration_denominators(self):
        overall = self.d["calibration"]["overall"]
        self.assertEqual(overall["anchor_n"], 1248 * 8)
        self.assertEqual(overall["transfer_n"], 1248 * 4)
        self.assertEqual(overall["anchor_expected_n"], 8)
        self.assertEqual(overall["transfer_expected_n"], 4)
        self.assertEqual(len(overall["bin_labels"]), 3)
        self.assertEqual(set(self.d["calibration"]["per_topic"]),
                         {"TOPIC_01", "TOPIC_02", "TOPIC_03"})

    def test_12_coverage_components_sum_to_100(self):
        for topic_id, node in self.d["coverage"].items():
            entry = node["T3"]
            self.assertIsNotNone(entry)
            total = entry["mastered_rate"] + entry["gap_rate"] + entry["missed_rate"]
            self.assertAlmostEqual(total, 100.0, places=6)
            self.assertEqual(node["u_part_order"],
                             ["复述(summary)", "论据(main_evidence)", "边界(boundary)"])
            self.assertEqual(set(entry["U_distribution"]),
                             {"0.0", "16.6667", "33.3333", "50.0", "66.6667", "83.3333", "100.0"})

    # ------------------------------------------------------------ 追溯
    def test_13_trace_paths_cover_full_chain(self):
        paths = self.d["trace_paths"]
        self.assertGreaterEqual(len(paths), 15)
        kinds = {path["kind"] for path in paths}
        self.assertTrue({"case_improved_T1", "case_improved_T3", "sampled",
                         "source_to_card", "effect"} <= kinds)
        for path in paths:
            types = [node["type"] for node in path["nodes"]]
            if path["kind"] != "source_to_card":
                self.assertIn("visible", types)
                self.assertIn("answer", types)
                self.assertIn("blind", types)
            self.assertEqual(types, sorted(types, key=lambda name: [
                "source", "evidence", "card", "visible", "answer", "blind",
                "network", "effect", "aggregate"].index(name)))

    def test_14_trace_answer_recomputes_to_panel_hash(self):
        path = next(p for p in self.d["trace_paths"] if p["kind"] == "case_improved_T3")
        answer = next(node for node in path["nodes"] if node["type"] == "answer")
        panel = {row["sid"]: row for row in self.d["panel"]}
        self.assertEqual(answer["value"]["output_sha256"], panel[answer["id"]]["msha"])
        visible = next(node for node in path["nodes"] if node["type"] == "visible")
        self.assertTrue(all(cid in {card["card_id"] for card in self.d["evidence"]["cards"]}
                            for cid in visible["value"]["card_ids"]))

    def test_15_verify_passes_with_25_paths(self):
        self.assertTrue(self.v["passed"])
        self.assertEqual(self.v["contract"], db.VERIFICATION_VERSION)
        self.assertEqual(self.v["views"], 11)
        self.assertGreaterEqual(self.v["trace_paths_verified"], 15)
        self.assertEqual(self.v["trace_paths_verified"], len(self.d["trace_paths"]))
        self.assertGreater(self.v["trace_nodes_verified"], 60)
        self.assertEqual(self.v["network_records_recomputed"], 156)
        self.assertEqual(self.v["panel_samples"], 1248)
        self.assertFalse(self.v["formal_eligible"])

    # ------------------------------------------------------------ HTML
    def test_16_eleven_views_and_banner_present(self):
        for view_id, title, _desc in db.VIEWS:
            self.assertIn(f'id="{view_id}"', self.html)
            self.assertIn(title, self.html)
            short = view_id.split("-")[1]
            self.assertIn(f'id="live-{short}"', self.html)
        self.assertIn(db.BANNER_TEXT, self.html)
        self.assertIn('id="top-banner"', self.html)
        self.assertLess(self.html.find('id="top-banner"'), self.html.find('id="view-01"'))

    def test_17_no_external_resources_or_credentials(self):
        stripped = re.sub(r'<script id="dashboard-data"[^>]*>.*?</script>', "", self.html, flags=re.S)
        for pattern in db._FORBIDDEN_LOAD:
            self.assertIsNone(pattern.search(stripped), pattern.pattern)
        self.assertNotIn("http://", stripped)
        self.assertNotIn("https://", stripped)
        self.assertIsNone(db._CREDENTIAL.search(self.html))
        self.assertEqual(self.html.count("<script"), 2)
        self.assertEqual(self.html.count("<style"), 1)

    def test_18_embedded_data_matches_file(self):
        match = re.search(r'<script id="dashboard-data"[^>]*>(.*?)</script>', self.html, flags=re.S)
        embedded = json.loads(match.group(1))
        self.assertEqual(embedded["dataset_sha256"], self.d["dataset_sha256"])
        self.assertEqual(embedded["banner"]["text"], db.BANNER_TEXT)

    def test_19_render_rejects_tampered_dataset(self):
        bad = deepcopy(self.d)
        bad["overview"]["panel_samples"] = 1
        with self.assertRaises(ValueError) as ctx:
            db.render_experiment_dashboard(self.__class__.tmp / "bad" / "x.html", bad)
        self.assertIn("dataset_hash_mismatch", str(ctx.exception))

    # ------------------------------------------------------------ 篡改拦截
    def test_20_tamper_dataset_without_rehash_rejected(self):
        target = self.__class__.tmp / "t20"
        target.mkdir()
        bad = deepcopy(self.d)
        bad["overview"]["worlds"] = 99
        (target / "dashboard_data.json").write_text(
            json.dumps(bad, ensure_ascii=False), encoding="utf-8", newline="\n")
        db.render_experiment_dashboard(target / "dashboard.html", self.d)
        self.reject(target, "dataset_hash_mismatch")

    def test_21_tamper_network_value_with_rehash_rejected(self):
        bad = deepcopy(self.d)
        bad["network_records"][0]["BS"] = bad["network_records"][0]["BS"] + 0.01
        resign(bad)
        target = self._write_case("t21", bad)
        self.reject(target, "network_mean_mismatch")

    def test_22_tamper_trace_answer_rejected(self):
        bad = deepcopy(self.d)
        path = next(p for p in bad["trace_paths"] if p["kind"] == "case_improved_T3")
        answer = next(node for node in path["nodes"] if node["type"] == "answer")
        answer["value"]["p_true"] = 0.0
        resign(bad)
        target = self._write_case("t22", bad)
        self.reject(target, "trace_answer_value_mismatch")

    def test_23_tamper_html_external_script_rejected(self):
        target = self._write_case("t23", self.d)
        html = (target / "dashboard.html").read_text(encoding="utf-8")
        html = html.replace("<body>", '<body><script src="https://cdn.example.com/x.js"></script>', 1)
        (target / "dashboard.html").write_text(html, encoding="utf-8")
        self.reject(target, "external_resource_load_detected")

    def test_24_tamper_banner_rejected(self):
        target = self._write_case("t24", self.d)
        html = (target / "dashboard.html").read_text(encoding="utf-8")
        html = html.replace(db.BANNER_TEXT, "示例数据，非实验结果", 1)
        (target / "dashboard.html").write_text(html, encoding="utf-8")
        self.reject(target, "banner_missing_in_html")

    def test_25_tamper_embedded_data_rejected(self):
        target = self._write_case("t25", self.d)
        html = (target / "dashboard.html").read_text(encoding="utf-8")
        html = html.replace('"panel_samples": 1248', '"panel_samples": 1249', 1)
        (target / "dashboard.html").write_text(html, encoding="utf-8")
        self.reject(target, "embedded_data_mismatch")

    # ------------------------------------------------------------ 原件与网络
    def test_26_protected_files_and_blank_template_unchanged(self):
        self.assertEqual(hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
                         self.template_before)
        checklist = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(checklist["status"], "pending_human_review")
        self.assertEqual(checklist["human_reviews_completed"], 0)
        self.assertFalse(checklist["formal_eligible"])
        for path in PROTECTED:
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             self.protected_before[path], str(path))

    def test_27_no_network_sockets(self):
        with mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")):
            result = db.verify_dashboard_bundle(self.__class__.case_dir)
        self.assertTrue(result["passed"])

    def test_28_missing_files_rejected(self):
        empty = self.__class__.tmp / "empty"
        empty.mkdir(exist_ok=True)
        self.reject(empty, "dataset_file_missing")

    # ------------------------------------------------------------ UI 布局防回归（步骤38）
    def test_29_chip_is_block_level_box(self):
        rule = db._css_rule(db._CSS, ".chip")
        self.assertIsNotNone(rule)
        display = re.search(r"display\s*:\s*([a-z-]+)", rule)
        self.assertIsNotNone(display)
        self.assertIn(display.group(1), ("inline-flex", "flex", "inline-block", "block"))
        self.assertIn("flex-direction:column", rule.replace(" ", ""))
        self.assertRegex(db._CSS, r"\.chip\s+small\s*\{[^}]*display\s*:\s*block")
        self.assertNotIn("border-radius:999px", rule)

    def test_30_static_timeline_uses_chips_container(self):
        self.assertGreaterEqual(self.html.count('class="timeline-row"'), 6)
        for row in re.finditer(r'<div class="timeline-row">(.*?)</div></div>', self.html, flags=re.S):
            self.assertIn('class="chips"', row.group(1))
        self.assertIn('class="timeline"', self.html)
        self.assertNotIn('<p><strong>TOPIC_', self.html)

    def test_31_dynamic_timeline_renderer_matches_static_layout(self):
        js = self.html[self.html.rfind("(function(){"):]
        start = js.find("function renderTimeline(){")
        self.assertGreaterEqual(start, 0)
        body = js[start:js.find("\n  }", start)]
        self.assertIn('class="timeline-row"', body)
        self.assertIn('class="chips"', body)
        self.assertNotIn('<p><strong>', body)

    def test_32_wide_tables_have_scroll_wrapper(self):
        self.assertIn('class="table-wrap"', db._table(["a"], [["b"]]))
        wrap = db._css_rule(db._CSS, ".table-wrap")
        self.assertIsNotNone(wrap)
        self.assertIn("overflow-x:auto", wrap.replace(" ", ""))
        live = db._css_rule(db._CSS, ".live")
        self.assertIn("overflow-x:auto", live.replace(" ", ""))
        self.assertGreaterEqual(self.html.count('class="table-wrap"'), 20)

    def test_33_network_grid_and_single_sticky_topbar(self):
        self.assertIn('class="net-grid"', self.html)
        self.assertEqual(self.html.count('class="net-card"'), 6)
        self.assertIn("grid-template-columns", db._css_rule(db._CSS, ".net-grid"))
        self.assertIn('class="topbar" id="topbar"', self.html)
        compact = re.sub(r"\s+", "", db._CSS)
        self.assertIn(".topbar{position:sticky", compact)
        self.assertNotIn(".filters{position:sticky", compact)
        self.assertNotIn("top:56px", compact)
        # banner 与 filters 必须同属一个 sticky 容器，避免折行时互相遮挡
        topbar = re.search(r'<div class="topbar" id="topbar">(.*?)</div>\s*<header>',
                           self.html, flags=re.S)
        self.assertIsNotNone(topbar)
        self.assertIn('id="top-banner"', topbar.group(1))
        self.assertIn('class="filters"', topbar.group(1))

    def test_34_ui_audit_reports_no_inline_block_conflicts(self):
        audit = db._ui_layout_audit(self.html)
        self.assertEqual(audit["chip_display"], "inline-flex")
        self.assertGreaterEqual(audit["timeline_rows"], 6)
        self.assertEqual(audit["network_cards"], 6)
        self.assertEqual(audit["checks"]["ui_inline_block_conflicts"], 0)
        self.assertTrue(self.v["passed"])
        self.assertEqual(self.v["ui_layout"]["chip_display"], "inline-flex")

    def test_35_tamper_chip_display_rejected(self):
        target = self._write_case("t35", self.d)
        html = (target / "dashboard.html").read_text(encoding="utf-8")
        html = html.replace("display:inline-flex;flex-direction:column", "display:inline", 1)
        (target / "dashboard.html").write_text(html, encoding="utf-8")
        self.reject(target, "ui_chip_display_not_block_level")

    def test_36_tamper_inline_block_nesting_rejected(self):
        target = self._write_case("t36", self.d)
        html = (target / "dashboard.html").read_text(encoding="utf-8")
        html = html.replace("<main>", '<main><span class="bad"><div>撕裂</div></span>', 1)
        (target / "dashboard.html").write_text(html, encoding="utf-8")
        self.reject(target, "ui_inline_block_conflict")

    def test_37_tamper_timeline_layout_rejected(self):
        target = self._write_case("t37", self.d)
        html = (target / "dashboard.html").read_text(encoding="utf-8")
        html = html.replace('class="timeline-row"', 'class="timeline-flat"')
        (target / "dashboard.html").write_text(html, encoding="utf-8")
        self.reject(target, "ui_timeline_rows_missing")

    # ------------------------------------------------------------ 步骤40 真实微型实证
    def test_38_live_micro_pilot_section_present(self):
        micro = self.d["live_micro_pilot"]
        self.assertIn(db._MICRO_TITLE, self.html)
        if micro["available"]:
            self.assertEqual(len(micro["comparison"]), 24)
            self.assertEqual(self.v["checks"]["live_micro_pilot_verified"], 24)
            self.assertIs(micro["honest_gates"]["formal_eligible"], False)
            for subject in micro["subjects"]:
                self.assertIn(db._esc(subject["speech"]), self.html)
            self.assertEqual(self.v["live_micro_pilot"]["manifest_sha256"], micro["manifest_sha256"])
        else:
            self.assertIn("真实微型实证尚未完成或未通过核验", self.html)
            self.assertIs(self.v["checks"]["live_micro_pilot_available"], False)

    def test_39_tamper_live_micro_pilot_rejected(self):
        bad = deepcopy(self.d)
        if bad["live_micro_pilot"]["available"]:
            bad["live_micro_pilot"]["comparison"][0]["BS"] = -1.0
            code = "live_micro_pilot_mismatch_comparison"
        else:
            bad.pop("live_micro_pilot")
            code = "live_micro_pilot_block_missing"
        resign(bad)
        target = self._write_case("t39", bad, render=False)
        db.render_experiment_dashboard(target / "dashboard.html", self.d)
        (target / "dashboard_data.json").write_text(
            json.dumps(bad, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8", newline="\n")
        with self.assertRaises(ValueError) as ctx:
            db.verify_dashboard_bundle(target)
        self.assertTrue(code in str(ctx.exception) or "embedded" in str(ctx.exception)
                        or "dataset" in str(ctx.exception), str(ctx.exception))


    # ------------------------------------------------------------ 步骤41 三议题真实矩阵
    def test_40_three_topic_matrix_section(self):
        matrix = self.d["live_micro_matrix"]
        self.assertIn(db._MATRIX_TITLE, self.html)
        if matrix["available"]:
            tot = matrix["totals"]
            self.assertEqual((tot["topics"], tot["branches"], tot["subject_measurements"],
                              tot["blind_evaluations"], tot["mirrors"], tot["intervention_responses"]),
                             (3, 18, 48, 48, 6, 36))
            self.assertGreaterEqual(self.html.count('class="mini-traj"'), 3)
            self.assertEqual(self.v["checks"]["live_micro_matrix_topics"], 3)
            for summary in matrix["topic_summaries"]:
                for subject in summary["subjects"]:
                    self.assertIn(db._esc(subject["speech"]), self.html)
            self.assertIs(matrix["honest_gates"]["formal_eligible"], False)
        else:
            self.assertIn("三议题矩阵尚未完成或未通过核验", self.html)
            self.assertIs(self.v["checks"]["live_micro_matrix_available"], False)

    def test_41_tamper_three_topic_matrix_rejected(self):
        bad = deepcopy(self.d)
        if bad["live_micro_matrix"]["available"]:
            bad["live_micro_matrix"]["totals"]["final_run_model_calls"] += 1
            code = "live_micro_matrix_mismatch_totals"
        else:
            bad.pop("live_micro_matrix")
            code = "live_micro_matrix_block_missing"
        resign(bad)
        target = self._write_case("t41", bad, render=False)
        db.render_experiment_dashboard(target / "dashboard.html", self.d)
        (target / "dashboard_data.json").write_text(
            json.dumps(bad, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8", newline="\n")
        with self.assertRaises(ValueError) as ctx:
            db.verify_dashboard_bundle(target)
        self.assertTrue(code in str(ctx.exception) or "embedded" in str(ctx.exception)
                        or "dataset" in str(ctx.exception), str(ctx.exception))

    # ------------------------------------------------------------ 步骤42 封闭情境 + 双轨对比 + 路演导览
    def _tamper(self, name, mutate):
        bad = deepcopy(self.d)
        mutate(bad)
        resign(bad)
        target = self._write_case(name, bad, render=False)
        db.render_experiment_dashboard(target / "dashboard.html", bad)
        with self.assertRaises(ValueError) as ctx:
            db.verify_dashboard_bundle(target)
        return str(ctx.exception)

    def test_42_closed_world_and_dual_track_sections(self):
        closed, dual = self.d["closed_world_pilot"], self.d["dual_track"]
        self.assertIs(closed["is_synthetic_closed_world"], True)
        self.assertIn(db._SYNTH_TITLE, self.html)
        self.assertIn(db._DUAL_TITLE, self.html)
        self.assertIn('id="spot-dualtrack"', self.html)
        self.assertIn('id="spot-synth"', self.html)
        if closed["available"]:
            self.assertEqual(len(closed["comparison"]), 24)
            self.assertEqual(self.v["checks"]["closed_world_verified"], 24)
            self.assertIs(closed["honest_gates"]["formal_eligible"], False)
            self.assertEqual(closed["honest_gates"]["protocol_basis"], "§5.4_closed_fictional_scenario")
            for subject in closed["subjects"]:
                self.assertIn(db._esc(subject["speech"]), self.html)
        else:
            self.assertIs(self.v["checks"]["closed_world_available"], False)
        if dual["available"]:
            self.assertEqual(self.html.count('class="dual-traj"'), 5)
            self.assertEqual(dual["metrics"], ["BS", "Acc", "S", "O", "U"])
            self.assertEqual([r["arm"] for r in dual["synthetic"]["by_arm"]], ["I0", "I5", "I4"])
        self.assertEqual(db._dual_track(self.d["live_micro_matrix"], closed), dual)

    def test_43_spotlight_tour_markup_and_targets(self):
        tour = self.d["spotlight_tour"]
        self.assertEqual(len(tour["steps"]), 7)
        self.assertEqual([s["step"] for s in tour["steps"]], list(range(1, 8)))
        for step in tour["steps"]:
            self.assertIn(f'id="{step["target"]}"', self.html)
            self.assertIn(f'data-step="{step["step"] - 1}"', self.html)
            self.assertTrue(step["notes"] and step["speaker"])
        self.assertIn('id="spotlight-tour"', self.html)
        self.assertIn('id="tour-notes"', self.html)
        self.assertIn("function initTour(){", self.html)
        self.assertEqual(self.v["checks"]["spotlight_tour_steps"], 7)
        self.assertEqual(self.html.count("<script"), 2)
        speakers = " ".join(s["speaker"] for s in tour["steps"])
        for role in ("A 实验控制", "B 数据与真值", "C 助手适配", "D 记录与可视化", "E 评分与统计"):
            self.assertIn(role, speakers)

    def test_44_tamper_dual_track_rejected(self):
        def mutate(bad):
            if bad["dual_track"].get("available"):
                bad["dual_track"]["synthetic"]["by_arm"][2]["BS_T3_mean"] = 0.0
            else:
                bad["dual_track"]["reason"] = "forged"
        self.assertIn("dual_track_mismatch", self._tamper("t44", mutate))

    def test_45_tamper_spotlight_tour_rejected(self):
        def mutate(bad):
            bad["spotlight_tour"]["steps"][4]["notes"][1] = "I4 全面碾压，效果显著（伪造）"
        self.assertIn("spotlight_tour_mismatch", self._tamper("t45", mutate))

    def test_46_tamper_closed_world_rejected(self):
        if not self.d["closed_world_pilot"]["available"]:
            def mutate(bad):
                bad["closed_world_pilot"]["is_synthetic_closed_world"] = False
            self.assertIn("closed_world_synthetic_flag_missing", self._tamper("t46", mutate))
            return
        def mutate(bad):
            bad["closed_world_pilot"]["comparison"][0]["BS"] = -1.0
        message = self._tamper("t46", mutate)
        self.assertTrue("closed_world_mismatch_comparison" in message or "dual_track" in message, message)


if __name__ == "__main__":
    unittest.main()
