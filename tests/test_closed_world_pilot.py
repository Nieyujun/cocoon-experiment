"""步骤42：§5.4 封闭虚构情境（TOPIC_SYNTH_01 星澜市）专项离线测试。

- 材料：确定性、三区隔离、误导卡不编码真假、来源为 .invalid 保留域、I0/I4/I5 剂量与配长可行。
- 全链路离线演练：socket 全禁，MiMoTransport.complete 由脚本化假响应替代（不写账本、不联网），
  在临时目录跑完 T0→T3→盲评→面板→清单→离线核验，并验证篡改拦截。
- 真实清单存在时：离线重放核验、诚实门禁与受保护原件不变。
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from cocoon import closed_world_pilot as cw
from cocoon import extractive_control, measurement, scheduling
from cocoon.contracts import STAGE_SECTIONS
from cocoon.mimo import MiMoTransport
from cocoon.runtime import canonical_hash

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / cw.MANIFEST_RELATIVE
LEDGER = ROOT / "experiment/billing/mimo_initial_probe_v1.jsonl"
PROTECTED = [
    ROOT / "experiment/review/live_micro_pilot_20261111/topic01_live_micro_pilot.json",
    ROOT / "experiment/review/live_micro_pilot_20261111/topic02_live_micro_pilot.json",
    ROOT / "experiment/review/live_micro_pilot_20261111/topic03_live_micro_pilot.json",
    ROOT / "experiment/review/live_micro_pilot_20261111/three_topic_live_micro_pilot_matrix.json",
    ROOT / "experiment/review/live_micro_pilot_20261111/REPORT_step41_20261113.md",
    ROOT / "experiment/review/cross_topic_20261108/human_review_checklist.json",
    ROOT / "破茧助手全量实验方案_v1.md",
]
FILLER = "核查证据显示相关数字要按统计口径和计入范围理解，不能直接套用讨论区流传的说法"


def _no_network():
    return (mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")),
            mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")))


def _organizer_reply(content):
    spec = json.loads(content.split("\n\n【上一次输出的校验反馈】")[0])
    ids, target = spec["evidence_ids"], spec["rendered_chars"]["target"]
    sentences = [{"text": "你此前采信的那个数字，出处是讨论区帖子还是核查来源？", "citations": [ids[0]]}]
    used = len(cw._render(sentences))
    while target - used > 0:
        cite = ids[len(sentences) % len(ids)]
        room = target - used - len(cite) - 2
        if room < 8:
            break
        text = (FILLER * 3)[:min(room, 60) - 1] + "。"
        sentences.append({"text": text, "citations": [cite]})
        used = len(cw._render(sentences))
    return {"sentences": sentences}


def _good_round(materials, r):
    window = materials["windows"][r]
    return _organizer_reply(json.dumps({"evidence_ids": [e[0] for e in cw.EVIDENCE if e[1] == r],
                                        "rendered_chars": {"target": (window["min"] + window["max"]) // 2}},
                                       ensure_ascii=False))


def _fake_complete(self, request):
    """脚本化假响应：只为离线演练链路结构，不代表任何模型行为。"""
    self.actual_model_calls += 1
    stage = request["stage"]
    content = request["messages"][-1]["content"]
    if stage in ("search", "label", "blindspot", "integrate", "mirror"):
        body = {"schema_version": "1.0", "stage": stage, "packet_id": "CAND_TOPIC_SYNTH_01_CLOSED_V1",
                "sections": [{"name": name, "items": [{"text": "这一点仍需对照核查来源。", "citations": [],
                                                       "epistemic_status": "uncertainty"}]}
                             for name in STAGE_SECTIONS[stage]]}
        raw = json.dumps(body, ensure_ascii=False)
    elif stage == "subject_measurement":
        answers = [{"item_id": i[0], "p_true": 0.5, "brief_reason": "目前读到的信息不足以判断。",
                    "cited_card_ids": []} for i in cw.ITEMS]
        raw = json.dumps({"answers": answers,
                          "evidence_notes": [{"known_evidence": "讨论区有不同说法。",
                                              "remaining_question": "需要核查来源确认数字。"}],
                          "opposing_view": {"summary": "另一方更看重长期体验。", "main_evidence": "他们提到舒适与运量。",
                                            "boundary": "这些看法依赖具体数字是否属实。"}}, ensure_ascii=False)
    elif stage == "routine_speech":
        raw = ("我看了这些讨论，觉得二期方案还要多看看数据再决定，网上的数字说法不一，"
               "我更想知道哪些是核实过的，哪些只是转发的说法，也想听听另一边的理由。") * 2
        raw = raw[:170]
    elif stage == "intervention_response":
        raw = "读完这些材料，我觉得原来的看法需要对照核查来源再想一想，有些数字我之前没有分清出处。"
    elif stage == "i4_organizer":
        raw = json.dumps(_organizer_reply(content), ensure_ascii=False)
    elif stage == "blind_evaluation":
        packet = json.loads(content)
        raw = json.dumps({"blind_sample_id": packet["blind_sample_id"],
                          "dimensions": [{"dimension_id": d["dimension_id"], "mastered": False,
                                          "recognized_gap": False, "evidence_quote": None}
                                         for d in packet["dimensions"]],
                          "opposing_scores": {"summary": 1, "main_evidence": 1, "boundary": 1,
                                              "rationale": "复述基本到位但缺少来源限定。"}}, ensure_ascii=False)
    else:
        raise AssertionError("unexpected_stage:" + stage)
    settings = request["model_settings"]
    return {"raw_text": raw, "provider": settings["provider"], "model": settings["model"],
            "response_id": "fake-" + hashlib.sha256(request["request_id"].encode()).hexdigest()[:12],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "finish_reason": "stop", "is_demo": False}


class MaterialsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = cw.build_closed_world_materials()

    def test_01_deterministic_and_flagged(self):
        again = cw.build_closed_world_materials()
        self.assertEqual(self.m["materials_sha256"], again["materials_sha256"])
        self.assertTrue(self.m["is_synthetic_closed_world"])
        self.assertEqual(cw.PROTOCOL_BASIS, "§5.4_closed_fictional_scenario")
        self.assertEqual(self.m["questionnaire"]["topic_id"], "TOPIC_SYNTH_01")

    def test_02_questionnaire_eight_plus_four_balanced(self):
        q = self.m["questionnaire"]
        self.assertEqual((len(q["anchor_items"]), len(q["transfer_items"])), (8, 4))
        truths = {i[0]: i[3] for i in cw.ITEMS}
        self.assertEqual(sum(truths[i["item_id"]] for i in q["anchor_items"]), 4)
        self.assertEqual(sum(truths[i["item_id"]] for i in q["transfer_items"]), 2)
        self.assertIn(len(cw.MISLED_ITEMS), (4, 5))
        self.assertTrue(all(cw.ITEMS[int(i[1:]) - 1][1] == "anchor" for i in cw.MISLED_ITEMS))

    def test_03_public_fields_hide_truth_and_stance(self):
        for subject in cw.SUBJECTS:
            for phase in ("exposure", "retention"):
                for batch in self.m["schedules"][subject][phase]:
                    self.assertEqual(len(batch), 6)
                    for card in batch:
                        self.assertNotIn("stance", card)
                        self.assertNotIn("is_accurate", card)
                        self.assertNotIn("targeted_items", card)
                        self.assertTrue(card["source_url"].startswith("https://forum.xinglan.synthetic.invalid/"))
                        self.assertEqual(card["transformation_type"], "synthetic_closed_world")
        ids = [c["card_id"] for c in self.m["cards"]]
        self.assertTrue(all(re.fullmatch(r"XLC\d{2}", i) for i in ids))

    def test_04_environment_d_quota_and_misleading_density(self):
        for subject in cw.SUBJECTS:
            audit = self.m["exposure_audit"][subject]["exposure"]
            self.assertEqual(audit["card_deliveries"], 48)
            self.assertEqual(audit["same_stance_deliveries"], 32)
            self.assertEqual(audit["misleading_card_deliveries"], 16)
            for item in cw.MISLED_ITEMS:
                self.assertGreaterEqual(audit["misleading_mentions_by_item"][item], 3)
            for item in cw.UNMISLED_ANCHORS:
                self.assertEqual(audit["misleading_mentions_by_item"][item], 0)

    def test_05_exposure_never_reveals_official_numbers(self):
        official = ("九千六百", "四千三百五十", "六千九百", "百分之十八", "百分之五十八", "百分之五十二",
                    "约九天", "约六天", "二十二个月", "三十四个月", "百分之八十七")
        for card in self.m["cards"]:
            for token in official:
                self.assertNotIn(token, card["text"])

    def test_06_misleading_cards_not_attributed_to_authority(self):
        for card in self.m["cards"]:
            if not card["is_accurate"]:
                self.assertIsNone(re.search(r"审计局|生态环境局|交通规划研究院", card["text"]))

    def test_07_packet_sources_are_reserved_invalid_domain(self):
        for entry in self.m["packet"]["evidence"]:
            self.assertRegex(entry["source_url"], r"^https://[a-z]+\.xinglan\.synthetic\.invalid/")
        self.assertFalse(self.m["packet"]["is_demo"])

    def test_08_i0_dose_and_no_new_numbers(self):
        for subject in cw.SUBJECTS:
            for r in cw.INTERVENTION_ROUNDS:
                text = self.m["i0"][subject][r]["visible_text"]
                self.assertTrue(600 <= len(text) <= 800)
                self.assertNotIn("阅读说明", text)
                self.assertNotIn("XL_E", text)

    def test_09_windows_feasible_for_extract_pairing(self):
        for r in cw.INTERVENTION_ROUNDS:
            w = self.m["windows"][r]
            self.assertLess(w["min"], w["max"])
            for length in (w["min"], (w["min"] + w["max"]) // 2, w["max"]):
                sel = extractive_control.select_exact_sentences(
                    cw._round_evidence(self.m["packet"], r), length, w["fixed_chars"], extractive_control._POLICY)
                self.assertEqual(sel["status"], "structurally_matched_candidate")

    def test_10_persona_contains_closed_world_rule(self):
        for subject in cw.SUBJECTS:
            self.assertIn("0.45到0.55", self.m["personas"][subject])
            self.assertIn("封闭评估案例", self.m["personas"][subject])

    def test_11_organizer_payload_hides_questionnaire_gold_dimensions(self):
        fake_output = {"sections": [{"name": "x", "items": [{"text": "甲"}]}]}
        previous = {"integrate": {"structured_output": fake_output}, "blindspot": {"structured_output": fake_output}}
        blob = json.dumps([cw.organizer_messages(self.m, "读者原话样例文字", fake_output, previous, r)
                           for r in cw.INTERVENTION_ROUNDS], ensure_ascii=False)
        for item in cw.ITEMS:
            self.assertNotIn(item[2], blob)
        for dim in cw.DIMENSIONS:
            self.assertNotIn(dim[1], blob)
        self.assertNotIn("truth", blob)

    def test_12_organizer_validator_rejects_bad_outputs(self):
        check = cw._organizer_validator(self.m, 1)
        self.assertEqual(check("不是JSON")[1], "closed_world_pilot:organizer_not_json")
        self.assertEqual(check(json.dumps({"rounds": []}))[1], "closed_world_pilot:organizer_shape_invalid")
        for r in cw.INTERVENTION_ROUNDS:
            value, error, _ = cw._organizer_validator(self.m, r)(json.dumps(_good_round(self.m, r), ensure_ascii=False))
            self.assertIsNone(error)
            self.assertTrue(value[r]["checks"]["assembly_valid"])
        good = _good_round(self.m, 1)
        half = deepcopy(good)
        half["sentences"][0]["text"] = half["sentences"][0]["text"].replace("？", "?")
        self.assertIsNone(check(json.dumps(half, ensure_ascii=False))[1])
        latin = deepcopy(good)
        latin["sentences"][0]["text"] = "这个数字来自XL_E01吗？"
        self.assertIn("latin_letters", check(json.dumps(latin, ensure_ascii=False))[2]["round_1"])
        noq = deepcopy(good)
        noq["sentences"][0]["text"] = "这句话不是提问。"
        self.assertTrue(check(json.dumps(noq, ensure_ascii=False))[2]["round_1"].get("missing_reflection_question"))
        badcite = deepcopy(good)
        badcite["sentences"][0]["citations"] = ["XL_E05"]
        self.assertIn("citations_invalid", check(json.dumps(badcite, ensure_ascii=False))[2]["round_1"])
        long = deepcopy(good)
        long["sentences"] += [{"text": FILLER + "。", "citations": ["XL_E01"]}] * 4
        diag = check(json.dumps(long, ensure_ascii=False))[2]["round_1"]
        self.assertIn("rendered_length", diag)
        feedback = cw._organizer_feedback("closed_world_pilot:organizer_rejected", {"round_1": diag})
        self.assertIn("删减", feedback["instruction"])

    def test_13_execute_required_zero_calls(self):
        with mock.patch.object(MiMoTransport, "complete", side_effect=AssertionError("no_call")):
            result = cw.run_closed_world_live_pilot(execute=False)
        self.assertFalse(result["executed"])
        self.assertEqual(result["actual_model_calls"], 0)
        self.assertTrue({"execute_required", "missing_MIMO_API_KEY"} & set(result["blockers"]))

    def test_14_summary_absent_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = cw.summarize_closed_world_live_pilot(manifest_path=Path(tmp) / "none.json")
        self.assertFalse(summary["available"])
        self.assertTrue(summary["is_synthetic_closed_world"])


class OfflineRehearsalTests(unittest.TestCase):
    """脚本化假响应全链路演练：结构、配对、注册表、面板、清单与核验（不联网、不记账）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.ledger_before = hashlib.sha256(LEDGER.read_bytes()).hexdigest()
        first, second = _no_network()
        env = {"MIMO_API_KEY": "sk-offline-rehearsal-not-a-real-key"}
        with first, second, mock.patch.dict(os.environ, env), \
                mock.patch.object(MiMoTransport, "complete", autospec=True, side_effect=_fake_complete):
            cls.result = cw.run_closed_world_live_pilot(execute=True, runs_root=tmp / "runs",
                                                        manifest_path=tmp / "manifest.json")
            cls.live_four = cw.run_closed_world_live_pilot(execute=True, runs_root=tmp / "runs_live4",
                                                           manifest_path=tmp / "manifest_live4.json",
                                                           four_stage_source=None)
        cls.manifest_path = tmp / "manifest.json"
        cls.manifest = json.loads(cls.manifest_path.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_19_live_four_stage_path_also_completes(self):
        self.assertEqual(self.live_four["status"], "completed", self.live_four.get("error"))
        self.assertEqual(self.live_four["actual_model_calls"], 56)
        manifest = json.loads((Path(self.tmp.name) / "manifest_live4.json").read_text(encoding="utf-8"))
        self.assertIsNone(manifest["four_stage"]["reused_from"])
        self.assertEqual(manifest["totals"]["four_stage_calls"], 4)
        check = cw.verify_closed_world_live_pilot(Path(self.tmp.name) / "manifest_live4.json")
        self.assertTrue(check["passed"], check["problems"])

    def test_20_run_completed_with_expected_calls(self):
        self.assertEqual(self.result["status"], "completed", self.result.get("error"))
        self.assertEqual(self.result["actual_model_calls"], 52)
        self.assertEqual(self.result["cost_cny"], "0.00000000")
        self.assertEqual(hashlib.sha256(LEDGER.read_bytes()).hexdigest(), self.ledger_before)

    def test_21_node_inventory(self):
        kinds = {}
        for node in self.manifest["nodes"]:
            kinds[node["kind"]] = kinds.get(node["kind"], 0) + 1
        self.assertEqual(kinds, {"subject_measurement": 16, "routine_speech": 2, "mirror": 2, "i4_organizer": 4,
                                 "intervention_response": 12, "blind_evaluation": 16})
        self.assertEqual(self.manifest["totals"]["four_stage_calls"], 0)
        reused = self.manifest["four_stage"]["reused_from"]
        self.assertEqual(reused["run_id"], cw.FOUR_STAGE_SOURCE["run_id"])
        self.assertEqual(set(reused["stages"]), {"search", "label", "blindspot", "integrate"})
        self.assertTrue(any(r["run_id"] == reused["run_id"] for r in self.manifest["superseded_runs"]))

    def test_22_offline_verification_passes(self):
        check = cw.verify_closed_world_live_pilot(self.manifest_path)
        self.assertTrue(check["passed"], check["problems"])
        self.assertEqual(check["measurements_revalidated"], 16)
        self.assertEqual(check["organizer_outputs_revalidated"], 4)
        self.assertTrue(check["is_synthetic_closed_world"])

    def test_23_pair_checks_all_true(self):
        for subject in cw.SUBJECTS:
            for r in ("1", "2"):
                checks = self.manifest["interventions"][subject]["pair_checks"][r]
                self.assertTrue(all(checks[k] for k in extractive_control._CHECK_NAMES))
                i4 = self.manifest["interventions"][subject]["I4"][r]
                i5 = self.manifest["interventions"][subject]["I5"][r]
                self.assertTrue(600 <= i4["char_count"] <= 800 and 600 <= i5["char_count"] <= 800)
                self.assertLessEqual(abs(i4["char_count"] - i5["char_count"])
                                     / min(i4["char_count"], i5["char_count"]), 0.1)
                block = self.manifest["interventions"][subject]["I4"][r]["visible_text"].split("阅读说明")[0]
                self.assertEqual(block, i5["visible_text"].split("阅读说明")[0])

    def test_24_honest_gates(self):
        gates = self.manifest["honest_gates"]
        self.assertTrue(gates["is_synthetic_closed_world"])
        self.assertEqual(gates["protocol_basis"], "§5.4_closed_fictional_scenario")
        self.assertFalse(gates["formal_eligible"])
        self.assertFalse(gates["real_world_evidence"])
        self.assertFalse(self.manifest["closed_world"]["organizer_sees_questionnaire_or_gold"])

    def test_25_panel_and_descriptive(self):
        self.assertEqual(len(self.manifest["panel_records"]), 16)
        self.assertEqual(len(self.manifest["comparison"]), 24)
        self.assertEqual(len(self.manifest["item_level"]), 16)
        self.assertEqual([r["arm"] for r in self.manifest["descriptive_by_arm"]], ["I0", "I5", "I4"])

    def _tampered(self, mutate):
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        mutate(data)
        data["manifest_sha256"] = canonical_hash({k: v for k, v in data.items() if k != "manifest_sha256"})
        path = Path(self.tmp.name) / ("tampered_%d.json" % abs(hash(str(mutate))))
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return cw.verify_closed_world_live_pilot(path)

    def test_26_tamper_honest_gate_detected(self):
        check = self._tampered(lambda d: d["honest_gates"].update(formal_eligible=True))
        self.assertIn("honest_gate_formal_eligible", check["problems"])
        check = self._tampered(lambda d: d["honest_gates"].update(is_synthetic_closed_world=False))
        self.assertIn("honest_gate_is_synthetic_closed_world", check["problems"])

    def test_27_tamper_intervention_detected(self):
        def mutate(d):
            d["interventions"]["S01"]["I5"]["1"]["visible_text"] += "补"
        self.assertIn("interventions_rebuild_mismatch", self._tampered(mutate)["problems"])

    def test_28_tamper_comparison_detected(self):
        def mutate(d):
            d["comparison"][0]["BS"] = 0.0
        self.assertIn("comparison_table_mismatch", self._tampered(mutate)["problems"])

    def test_29_tamper_materials_hash_detected(self):
        def mutate(d):
            d["closed_world"]["materials_sha256"] = "0" * 64
        self.assertIn("materials_rebuild_mismatch", self._tampered(mutate)["problems"])

    def test_29b_tamper_four_stage_reuse_detected(self):
        def mutate(d):
            d["four_stage"]["outputs"]["integrate"]["sections"][0]["items"] = []
        self.assertIn("four_stage_reuse_output_mismatch", self._tampered(mutate)["problems"])

    def test_30_summary_for_dashboard(self):
        summary = cw.summarize_closed_world_live_pilot(manifest_path=self.manifest_path)
        self.assertTrue(summary["available"], summary.get("problems"))
        self.assertEqual(len(summary["subjects"]), 2)
        self.assertIn("1", summary["subjects"][0]["i4_additions"])
        self.assertEqual(set(summary["subjects"][0]["intervention_responses"]), set(cw.ARMS))


if MANIFEST.is_file():
    class LiveManifestReplayTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.protected_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
            first, second = _no_network()
            with first, second:
                cls.verification = cw.verify_closed_world_live_pilot(MANIFEST)
                cls.summary = cw.summarize_closed_world_live_pilot()
            cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

        @classmethod
        def tearDownClass(cls):
            after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
            if after != cls.protected_before:
                raise AssertionError("protected_files_changed")

        def test_40_live_manifest_verifies(self):
            self.assertTrue(self.verification["passed"], self.verification["problems"])
            self.assertEqual(self.verification["measurements_revalidated"], 16)

        def test_41_live_honest_gates(self):
            gates = self.manifest["honest_gates"]
            self.assertTrue(gates["is_synthetic_closed_world"])
            self.assertFalse(gates["formal_eligible"])
            self.assertEqual(gates["protocol_basis"], "§5.4_closed_fictional_scenario")
            self.assertEqual(gates["real_human_subjects"], 0)

        def test_42_live_panel_complete_and_ledger_reconciled(self):
            self.assertEqual(len(self.manifest["panel_records"]), 16)
            self.assertGreaterEqual(self.manifest["totals"]["actual_model_calls"], 52)
            self.assertEqual(self.manifest["totals"]["ledger_settles"], self.manifest["totals"]["actual_model_calls"])
            self.assertGreater(float(self.manifest["totals"]["charged_cny"]), 0)

        def test_43_live_summary_available(self):
            self.assertTrue(self.summary["available"])
            self.assertTrue(all(s["speech"] for s in self.summary["subjects"]))


if __name__ == "__main__":
    unittest.main()
