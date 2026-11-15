"""步骤30：全部问卷、真值和模型输出均为显式合成内存夹具，禁止落盘/联网。"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import socket
import unittest
from unittest import mock

from cocoon import measurement as m, metrics, runtime
from cocoon.contracts import STAGE_SECTIONS, parse_stage_output

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "experiment/review/cross_topic_20261108"
PROTECTED = [BASE / name for name in ("feasibility.json", "human_review_checklist.json", "independent_review.json",
                                    "review.html", "delivery.json", "REPORT_step28_20261108.md", "REPORT_step29_20261108.md")]
PROTECTED += [ROOT / "experiment/billing/mimo_initial_probe_v1.jsonl", ROOT / "破茧助手全量实验方案_v1.md"]
PROTECTED += [ROOT / "cocoon" / name for name in ("metrics.py", "runtime.py", "contracts.py", "scheduling.py", "human_review.py")]
PROTECTED += [ROOT / "experiment/review/measurement_20261108/REPORT_step30_20261108.md",
              ROOT / "cocoon/extractive_control.py"]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def fixture_questionnaire(transfer=True):
    q = {"schema_version": "1.0", "questionnaire_id": "QSET01", "topic_id": "SYNTHETIC", "is_demo": True,
         "anchor_items": [{"item_id": f"Q{i:02d}", "statement": f"合成内存样例命题{i}，不表示现实世界事实。"} for i in range(1, 9)]}
    if transfer:
        q["transfer_items"] = [{"item_id": f"Q{i:02d}", "statement": f"合成迁移样例命题{i}，不是正式试题。"} for i in range(9, 13)]
    return q


def fixture_gold(q):
    return {"schema_version": "1.0", "questionnaire_id": q["questionnaire_id"], "topic_id": q["topic_id"], "is_demo": q["is_demo"],
            "gold_answers": [{"item_id": item["item_id"], "partition": group, "truth": int(group == "transfer")}
                             for key, group in (("anchor_items", "anchor"), ("transfer_items", "transfer")) for item in q.get(key, [])],
            "dimensions": [{"dimension_id": f"D{i:02d}", "name": f"合成指标{i}", "criteria": f"只检验封闭样例条件{i}是否说明。"} for i in range(1, 7)],
            "opposing_rubric": {key: {"0": "合成量规：没有说明。", "1": "合成量规：有部分说明。", "2": "合成量规：完整说明。"}
                                 for key in ("summary", "main_evidence", "boundary")}}


def fixture_output(q):
    return {"answers": [{"item_id": item["item_id"], "p_true": 0, "brief_reason": f"样例自报依据编号{n}，尚需核查完整边界。",
                          "cited_card_ids": ["CARD_01"]} for n, item in enumerate(q["anchor_items"] + q.get("transfer_items", []), 1)],
            "evidence_notes": [{"known_evidence": "封闭样例有甲类记录，不是现实观察。", "remaining_question": "尚需检查样例适用边界。"}],
            "opposing_view": {"summary": "另一观点重视样例的适用条件。", "main_evidence": "其论据来自另一项封闭样例说明。", "boundary": "该观点只适用于这些样例假设。"}}


def fixture_card(number=1):
    return {"card_id": f"CARD_{number:02d}", "topic_id": "SYNTHETIC", "text": "合成可见卡片，不代表实际受试观测。",
            "source_family_id": f"F{number:02d}", "source_url": "https://example.invalid/synthetic",
            "transformation_type": "synthetic", "is_demo": True}


def fixture_event(number=1, agent="S01", delivered=True, card=None):
    return {"event_id": f"E{number:02d}", "event_type": "exposure_delivered" if delivered else "exposure_prepared",
            "agent_id": agent, "status": "delivered" if delivered else "prepared", "sent_to_model": delivered,
            "cards": [fixture_card(number) if card is None else card]}


def fixture_world():
    return {"world_id": "W01", "environment": "balanced", "arm": "I4", "seed": 1, "timepoint": "T1", "is_demo": True,
            "agents": [{"agent_id": "S01", "initial_stance": 1}, {"agent_id": "S02", "initial_stance": -1}],
            "messages": {"S01": [{"role": "user", "content": "仅供内存测试的可见历史。"}],
                         "S02": [{"role": "user", "content": "另一人的私有发言。"}]},
            "exposure_history": [fixture_event(1), fixture_event(2, delivered=False), fixture_event(3, agent="S02")],
            "memory_summaries": {"S01": "控制器记忆，不自动发送。"}, "prepared_inputs": [fixture_card(4)],
            "gold_bundle": {"private_marker": "不得传给受试"}}


def fixture_blind_output(packet):
    flags = [(True, True), (True, False), (False, True), (False, True), (False, False), (False, False)]
    quote = packet["evidence_notes"][0]["known_evidence"]
    return {"blind_sample_id": packet["blind_sample_id"],
            "dimensions": [{"dimension_id": dim["dimension_id"], "mastered": mastered, "recognized_gap": gap,
                            "evidence_quote": quote if mastered or gap else None}
                           for dim, (mastered, gap) in zip(packet["dimensions"], flags)],
            "opposing_scores": {"summary": 1, "main_evidence": 2, "boundary": 0, "rationale": "仅依据样例文字及量规评定。"}}


def fixture_routine_speech(number=1, *, agent="S01", topic="SYNTHETIC", round_no=None):
    return {"speech_id": f"SP{number:02d}", "event_id": f"RE{number:02d}", "world_id": "W01",
            "agent_id": agent, "topic_id": topic, "phase": "exposure", "round": number if round_no is None else round_no,
            "logical_seq": number * 10, "event_type": "discussion_reply", "role": "assistant",
            "status": "completed", "generated": True, "text": f"常规讨论样例{number}：我想核对具体证据及适用条件。",
            "is_demo": True}


def fixture_routine_world():
    world = fixture_world()
    world.update(round=8, logical_seq=100, routine_speech_history=[fixture_routine_speech(2), fixture_routine_speech(1)])
    return world


def fixture_registry_entries(n=8):
    q = fixture_questionnaire()
    private = m.validate_private_gold_bundle(fixture_gold(q), q)
    entries = []
    for i in range(n):
        point = ("T0", "T1", "T2", "T3")[i % 4]
        out = fixture_output(q)
        out["evidence_notes"][0]["known_evidence"] += f"独立样例文字序号{i}。"
        out["answers"][0]["p_true"] = (i % 3) / 2
        record = m.parse_subject_measurement_output(dumps(out), q, visible_card_ids=["CARD_01"], timepoint=point)
        entries.append({"parsed_measurement": record, "gold_bundle": deepcopy(private), "world_id": f"W{i // 4:02d}",
                        "agent_id": "S01", "environment": "D", "arm": "I4" if i // 4 == 0 else "I5",
                        "timepoint": point, "block_id": "B01"})
    return entries


def registry_measurements(registry):
    return {sid: deepcopy(registry["source_entries"][value["source_entry_index"]]["parsed_measurement"])
            for sid, value in registry["private_mapping"].items()}


def registry_evaluations(registry):
    return [m.parse_blind_evaluation_output(dumps(fixture_blind_output(packet)), packet)
            for packet in registry["public_blind_packets"]]


class MeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
        cls.review_files = set(BASE.iterdir())

    @classmethod
    def tearDownClass(cls):
        if cls.before != {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in cls.before}:
            raise AssertionError("保护的原文件发生改变")
        if set(BASE.iterdir()) != cls.review_files:
            raise AssertionError("测试不得向旧产物目录写入文件")

    def setUp(self):
        self.q = fixture_questionnaire()
        self.gold = fixture_gold(self.q)
        self.out = fixture_output(self.q)
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("禁止网络请求"))
        self.net = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.net.assert_not_called)
        real_open = Path.open

        def read_only(path, mode="r", *args, **kwargs):
            if any(c in mode for c in "wax+"):
                raise AssertionError("禁止落盘合成金标准或受试结果")
            return real_open(path, mode, *args, **kwargs)

        patcher = mock.patch.object(Path, "open", read_only)
        patcher.start()
        self.addCleanup(patcher.stop)

    def parsed(self, out=None, q=None, point="T1", visible=None):
        return m.parse_subject_measurement_output(dumps(self.out if out is None else out), self.q if q is None else q,
                                                 visible_card_ids=["CARD_01"] if visible is None else visible, timepoint=point)

    def private(self, gold=None, q=None):
        return m.validate_private_gold_bundle(self.gold if gold is None else gold, self.q if q is None else q)

    def blind(self, record=None, private=None, sid="CASE_abc"):
        return m.build_blind_evaluation_packet(self.parsed() if record is None else record,
                                              self.private() if private is None else private, blind_sample_id=sid)

    def evaluated(self, packet=None, output=None):
        packet = self.blind() if packet is None else packet
        return m.parse_blind_evaluation_output(dumps(fixture_blind_output(packet) if output is None else output), packet)

    def reject(self, function, *args, **kwargs):
        with self.assertRaisesRegex(ValueError, "^measurement:"):
            function(*args, **kwargs)

    def test_01_public_questionnaire_deepcopy_eight_plus_optional_four(self):
        before = deepcopy(self.q)
        validated = m.validate_public_questionnaire(self.q)
        self.assertEqual(validated, self.q)
        validated["anchor_items"][0]["statement"] = "改变副本"
        self.assertEqual(self.q, before)
        q = fixture_questionnaire(False)
        result = m.validate_public_questionnaire(q)
        self.assertEqual(result["transfer_items"], [])
        self.assertNotIn("transfer_items", q)
        self.assertNotIn("dimensions", result)

    def test_02_public_counts_types_and_duplicates_rejected(self):
        changes = [lambda q: q["anchor_items"].pop(), lambda q: q["anchor_items"].append(q["anchor_items"][0]),
                   lambda q: q["transfer_items"].pop(), lambda q: q.update(is_demo=1), lambda q: q.update(schema_version=1.0),
                   lambda q: q["anchor_items"][1].update(item_id="Q01"), lambda q: q["transfer_items"][0].update(item_id="Q01")]
        for change in changes:
            q = deepcopy(self.q)
            change(q)
            self.reject(m.validate_public_questionnaire, q)

    def test_03_public_private_keys_at_any_level_rejected(self):
        for key in ("gold", "truth", "is_accurate", "stance", "answer", "analysis", "真假", "答案", "dimensions", "opposing_rubric", "api_key"):
            for at_top in (True, False):
                q = deepcopy(self.q)
                target = q if at_top else q["anchor_items"][0]
                target[key] = "不应进入公共问卷"
                with self.subTest(key=key, at_top=at_top):
                    self.reject(m.validate_public_questionnaire, q)

    def test_04_label_carrying_identifiers_rejected(self):
        for value in ("Q_gold", "Qtruth", "is_accurate", "goldAnswer", "STANCE_1", "analysis", "Q%5fgold", "真假", "答案", "Q_true"):
            for field in ("questionnaire_id", "topic_id", "item_id"):
                q = deepcopy(self.q)
                target = q["anchor_items"][0] if field == "item_id" else q
                target[field] = value
                with self.subTest(field=field, value=value):
                    self.reject(m.validate_public_questionnaire, q)

    def test_05_plain_text_html_encoded_controls_and_unicode(self):
        for value in ("", "  ", "<b>文本</b>", "&lt;b&gt;文本&lt;/b&gt;", "<!--说明-->", "正文\x00", "正文\n", "正文\u200b", "\ud800"):
            q = deepcopy(self.q)
            q["anchor_items"][0]["statement"] = value
            self.reject(m.validate_public_questionnaire, q)
        q = deepcopy(self.q)
        q["anchor_items"][0]["statement"] = "数值<2时只陈述条件，真假待受试者判断。"
        self.assertEqual(m.validate_public_questionnaire(q)["anchor_items"][0]["statement"], q["anchor_items"][0]["statement"])

    def test_06_private_bundle_bound_and_no_shared_mutation(self):
        private = self.private()
        self.assertEqual(private["bundle"], self.gold)
        self.assertEqual(private["bundle_sha256"], runtime.canonical_hash(self.gold))
        self.assertTrue(private["private_only"])
        private["bundle"]["gold_answers"][0]["truth"] = 1
        self.assertEqual(self.gold["gold_answers"][0]["truth"], 0)
        self.assertNotIn("dimensions", self.q)

    def test_07_gold_exact_coverage_partitions_and_binary_int(self):
        changes = [lambda g: g["gold_answers"].pop(), lambda g: g["gold_answers"].append(g["gold_answers"][0]),
                   lambda g: g["gold_answers"][1].update(item_id="Q01"), lambda g: g["gold_answers"][0].update(item_id="Q99"),
                   lambda g: g["gold_answers"][0].update(partition="transfer"), lambda g: g.update(questionnaire_id="QOTHER"),
                   lambda g: g.update(is_demo=False)]
        for change in changes:
            gold = deepcopy(self.gold)
            change(gold)
            self.reject(m.validate_private_gold_bundle, gold, self.q)
        for truth in (True, False, -1, 2, 0.0, "1", None):
            gold = deepcopy(self.gold)
            gold["gold_answers"][0]["truth"] = truth
            self.reject(m.validate_private_gold_bundle, gold, self.q)

    def test_08_six_dimensions_unique_and_rubric_exact(self):
        changes = [lambda g: g["dimensions"].pop(), lambda g: g["dimensions"][1].update(dimension_id="D01"),
                   lambda g: g["dimensions"][0].update(criteria=""), lambda g: g["dimensions"][0].update(extra="leak"),
                   lambda g: g["opposing_rubric"]["summary"].pop("2"), lambda g: g["opposing_rubric"]["summary"].update({"3": "额外"}),
                   lambda g: g["opposing_rubric"].update(extra={}), lambda g: g["opposing_rubric"]["boundary"].update({"0": 0})]
        for change in changes:
            gold = deepcopy(self.gold)
            change(gold)
            self.reject(m.validate_private_gold_bundle, gold, self.q)

    def test_09_context_deepcopy_no_writes_private_world_not_sent(self):
        world = fixture_world()
        before = deepcopy(world)
        with mock.patch.object(runtime, "measurement_view", wraps=runtime.measurement_view) as view:
            context = m.prepare_subject_measurement_context(world, "S01", self.q, allow_demo=True)
        view.assert_called_once_with(world)
        self.assertEqual(world, before)
        self.assertEqual(context["world_state_before_sha256"], context["world_state_after_sha256"])
        self.assertFalse(context["measurement_writes_to_memory"])
        self.assertEqual(context["visible_card_ids"], ["CARD_01"])
        self.assertEqual(context["source_exposure_event_ids"], ["E01"])
        wire = dumps(context["messages"])
        for secret in ("另一人的私有发言", "gold_bundle", "initial_stance", "balanced", "world_id", "memory_summaries"):
            self.assertNotIn(secret, wire)
        context["messages"][0]["content"] = "改变返回副本"
        context["visible_card_ids"].append("C99")
        self.assertEqual(world, before)

    def test_10_context_demo_timepoints_unknown_agent_and_write_flag(self):
        world = fixture_world()
        self.reject(m.prepare_subject_measurement_context, world, "S01", self.q)
        self.reject(m.prepare_subject_measurement_context, world, "S99", self.q, allow_demo=True)
        for point in ("T0", "T1", "T2", "T3"):
            world["timepoint"] = point
            self.assertEqual(m.prepare_subject_measurement_context(world, "S01", self.q, allow_demo=True)["timepoint"], point)
        for key, value in (("measurement_writes_to_memory", True), ("timepoint", None), ("timepoint", "T4"), ("is_demo", False), ("is_demo", 1)):
            world = fixture_world()
            world[key] = value
            self.reject(m.prepare_subject_measurement_context, world, "S01", self.q, allow_demo=True)

    def test_11_prepared_inventory_or_other_agent_never_visible(self):
        world = fixture_world()
        world["exposure_history"] = world["exposure_history"][1:]
        context = m.prepare_subject_measurement_context(world, "S01", self.q, allow_demo=True)
        self.assertEqual(context["visible_card_ids"], [])
        world.pop("exposure_history")
        self.assertEqual(m.prepare_subject_measurement_context(world, "S01", self.q, allow_demo=True)["visible_card_ids"], [])

    def test_12_exposure_contradictions_duplicates_and_version_drift_rejected(self):
        changes = [lambda w: w["exposure_history"][0].update(sent_to_model=False),
                   lambda w: w["exposure_history"][0].update(sent_to_model=1),
                   lambda w: w["exposure_history"].append(deepcopy(w["exposure_history"][0])),
                   lambda w: w["exposure_history"][0]["cards"].append(deepcopy(w["exposure_history"][0]["cards"][0])),
                   lambda w: w["exposure_history"][0]["cards"][0].update(truth=1),
                   lambda w: w["agents"].append(deepcopy(w["agents"][0]))]
        for change in changes:
            world = fixture_world()
            change(world)
            self.reject(m.prepare_subject_measurement_context, world, "S01", self.q, allow_demo=True)
        world = fixture_world()
        second = fixture_event(8, card=fixture_card(1))
        world["exposure_history"].append(second)
        self.assertEqual(m.prepare_subject_measurement_context(world, "S01", self.q, allow_demo=True)["visible_card_ids"], ["CARD_01"])
        second["cards"][0]["text"] += "变化"
        self.reject(m.prepare_subject_measurement_context, world, "S01", self.q, allow_demo=True)

    def test_13_serialized_private_metadata_in_history_rejected(self):
        for content in ('{"gold_answers": [1]}', "dimensions: 私有评分维度", "金标准：仅评估器可见"):
            world = fixture_world()
            world["messages"]["S01"][0]["content"] = content
            self.reject(m.prepare_subject_measurement_context, world, "S01", self.q, allow_demo=True)

    def test_14_strict_subject_json_and_sensitive_keys(self):
        raw = dumps(self.out)
        invalid = ["```json\n" + raw + "\n```", raw + " text", raw.replace('"p_true": 0', '"p_true": 0, "p_true": 0', 1),
                   raw.replace('"p_true": 0', '"p_true": NaN', 1), raw.replace('"p_true": 0', '"p_true": Infinity', 1),
                   raw.replace('"p_true": 0', '"p_true": 1e999', 1), "[]"]
        for text in invalid:
            self.reject(m.parse_subject_measurement_output, text, self.q, visible_card_ids=["CARD_01"])
        for key in ("gold", "p_true", "api_key", "dimensions", "arm"):
            out = deepcopy(self.out)
            out[key] = "forbidden"
            self.reject(m.parse_subject_measurement_output, dumps(out), self.q, visible_card_ids=["CARD_01"])

    def test_15_subject_output_partial_unknown_duplicate(self):
        out = deepcopy(self.out)
        out["answers"] = out["answers"][:2]
        parsed = self.parsed(out)
        self.assertEqual(len(parsed["output"]["answers"]), 2)
        for answer in ({**out["answers"][0], "item_id": "Q99"}, out["answers"][0]):
            bad = deepcopy(out)
            bad["answers"].append(answer)
            self.reject(self.parsed, bad)
        out["answers"] = []
        self.assertEqual(self.parsed(out)["output"]["answers"], [])

    def test_16_probability_choice_and_citations_strict(self):
        for value in (True, False, -0.01, 1.01, "0.5", None):
            out = deepcopy(self.out)
            out["answers"][0]["p_true"] = value
            self.reject(self.parsed, out)
        for choice in (-1, 2, 0.0, "true", None):
            out = deepcopy(self.out)
            out["answers"][0]["choice"] = choice
            self.reject(self.parsed, out)
        for choice in (True, False, 0, 1):
            out = deepcopy(self.out)
            out["answers"][0]["choice"] = choice
            self.assertIn("choice", self.parsed(out)["output"]["answers"][0])
        for cites in (["CARD_02"], ["CARD_01", "CARD_01"], ["GOLD_01"], "CARD_01"):
            out = deepcopy(self.out)
            out["answers"][0]["cited_card_ids"] = cites
            self.reject(self.parsed, out)
        self.reject(self.parsed, visible=["CARD_01", "CARD_01"])

    def test_17_notes_opposing_schema_size_and_plaintext(self):
        changes = [lambda o: o.update(evidence_notes=[]), lambda o: o.update(evidence_notes=o["evidence_notes"] * 31),
                   lambda o: o["evidence_notes"][0].update(remaining_question=""), lambda o: o["evidence_notes"][0].update(dimension_id="D01"),
                   lambda o: o["opposing_view"].pop("boundary"), lambda o: o["opposing_view"].update(extra="x"),
                   lambda o: o["opposing_view"].update(summary="<p>文本</p>"), lambda o: o["answers"][0].update(brief_reason="\n"),
                   lambda o: o["answers"][0].update(brief_reason="字" * 4001)]
        for change in changes:
            out = deepcopy(self.out)
            change(out)
            self.reject(self.parsed, out)

    def test_18_record_mutation_not_trusted_by_bridge_blind_or_score(self):
        parsed = self.parsed()
        for change in (lambda r: r["output"]["answers"][0].update(p_true=1), lambda r: r.update(output_sha256="0" * 64),
                       lambda r: r.update(is_demo=False), lambda r: r.update(actual_observation_verified=True),
                       lambda r: r["source_event_ids"].append("E01")):
            wrong = deepcopy(parsed)
            change(wrong)
            self.reject(m.extract_t1_natural_view, wrong, self.q)
            self.reject(m.build_blind_evaluation_packet, wrong, self.private(), blind_sample_id="CASE_abc")
            self.reject(m.score_measurement_record, wrong, self.private())

    def test_19_bridge_exact_substrings_hash_and_no_objective_metadata(self):
        parsed = self.parsed()
        bridge = m.extract_t1_natural_view(parsed, self.q)
        view = bridge["user_view"]
        original = [a["brief_reason"] for a in self.out["answers"]]
        original += [n[k] for n in self.out["evidence_notes"] for k in ("known_evidence", "remaining_question")]
        original += list(self.out["opposing_view"].values())
        self.assertTrue(all(s in view for s in original))
        self.assertEqual(bridge["user_view_sha256"], hashlib.sha256(view.encode()).hexdigest())
        self.assertEqual(bridge["content_origin"], "subject_measurement_extracted")
        self.assertFalse(bridge["natural_routine_speech"] or bridge["actual_observation_verified"] or bridge["formal_eligible"])
        self.assertTrue(bridge["requires_protocol_review"])
        for text in ("p_true", "choice", "Q01", "gold", "T1", "I4", "world_id"):
            self.assertNotIn(text, view)
        self.assertEqual(m.extract_t1_natural_view(parsed, self.q), bridge)

    def test_20_bridge_requires_explicit_T1_and_matching_questionnaire(self):
        for point in (None, "T0", "T2", "T3"):
            self.reject(m.extract_t1_natural_view, self.parsed(point=point), self.q)
        q = deepcopy(self.q)
        q["anchor_items"][0]["statement"] += "变更版本"
        self.reject(m.extract_t1_natural_view, self.parsed(), q)
        self.reject(self.parsed, point="T4")

    def test_21_bridge_to_mirror_contract_end_to_end(self):
        record = self.parsed()
        bridge = m.extract_t1_natural_view(record, self.q)
        evidence = "合成证据仅为接口验证，不代表真实结论。"
        packet = {"schema_version": "1.0", "packet_id": "P01", "topic_id": "SYNTHETIC", "topic": "合成接口样例",
                  "stance_axis": {"name": "合成权重", "negative_label": "条件甲", "positive_label": "条件乙"}, "is_demo": True,
                  "evidence": [{"evidence_id": "E01", "text": evidence, "source_url": "https://example.invalid/synthetic",
                                "source_family_id": "F01", "content_sha256": hashlib.sha256(evidence.encode()).hexdigest()}]}
        quote = self.out["answers"][0]["brief_reason"]
        response = {"schema_version": "1.0", "stage": "mirror", "packet_id": "P01",
                    "sections": [{"name": n, "items": []} for n in STAGE_SECTIONS["mirror"]]}
        response["sections"][0]["items"] = [{"text": "该自报观点需要在证据边界内理解。", "citations": ["E01"],
                                              "epistemic_status": "evidence_based", "user_quote": quote}]
        result = parse_stage_output(dumps(response), "mirror", packet, user_view=bridge["user_view"])
        self.assertTrue(result["citation_audit"]["schema_valid"])
        self.assertFalse(result["citation_audit"]["formal_eligible"])
        response["sections"][0]["items"][0]["user_quote"] = "不存在于自报中的引文"
        with self.assertRaises(ValueError):
            parse_stage_output(dumps(response), "mirror", packet, user_view=bridge["user_view"])

    def test_22_bridge_canonical_question_order_despite_response_order(self):
        out = deepcopy(self.out)
        out["answers"].reverse()
        a = m.extract_t1_natural_view(self.parsed(out), self.q)
        b = m.extract_t1_natural_view(self.parsed(), self.q)
        self.assertEqual(a["user_view"], b["user_view"])
        self.assertEqual(a["user_view_sha256"], b["user_view_sha256"])

    def test_23_blind_packet_exact_five_fields_no_objective_halo(self):
        private, record = self.private(), self.parsed()
        packet = self.blind(record, private)
        self.assertEqual(set(packet), {"blind_sample_id", "evidence_notes", "opposing_view", "dimensions", "opposing_rubric"})
        self.assertEqual(packet["evidence_notes"], self.out["evidence_notes"])
        text = dumps(packet)
        for value in ("gold_answers", "answers", "p_true", "questionnaire_id", "is_demo", "timepoint", "source_event_ids", "T1", "CARD_01"):
            self.assertNotIn(value, text)
        packet["dimensions"][0]["criteria"] = "修改返回值"
        self.assertNotEqual(packet["dimensions"], private["bundle"]["dimensions"])

    def test_24_blind_ids_block_conditions_timepoints_agents(self):
        for sid in ("I4", "T3", "sample_T0", "CASE_I5", "world_id", "World01", "block_X", "seed_3", "S01", "homogeneous", "balanced",
                    "env_A", "xWorldId", "X_WORLD", "sampleT3", "CASE_GOLD"):
            with self.subTest(sid=sid):
                self.reject(self.blind, sid=sid)

    def test_25_blind_text_condition_leaks_in_all_input_sections_rejected(self):
        for marker in ("I4", "T3", "world_id", "S01", "seed", "balanced", "homogeneous", "env", "block_id", "实验组", "Ｔ３", "%49%34"):
            for field in ("notes", "opposing", "dimensions", "rubric"):
                out, gold = deepcopy(self.out), deepcopy(self.gold)
                if field == "notes":
                    out["evidence_notes"][0]["known_evidence"] += " " + marker
                elif field == "opposing":
                    out["opposing_view"]["boundary"] += " " + marker
                elif field == "dimensions":
                    gold["dimensions"][0]["criteria"] += " " + marker
                else:
                    gold["opposing_rubric"]["boundary"]["0"] += " " + marker
                with self.subTest(field=field, marker=marker):
                    self.reject(m.build_blind_evaluation_packet, self.parsed(out), self.private(gold), blind_sample_id="CASE_abc")

    def test_26_gold_and_questionnaire_tampering_rejected_before_blinding(self):
        private = self.private()
        private["bundle"]["gold_answers"][0]["truth"] = 1
        self.reject(self.blind, private=private)
        q = deepcopy(self.q)
        q["questionnaire_id"] = "QSET02"
        alternate = self.private(fixture_gold(q), q)
        self.reject(self.blind, private=alternate)
        self.reject(m.score_measurement_record, self.parsed(), alternate)
        self.reject(self.blind, private=self.gold)

    def test_27_blind_json_sample_order_six_dimensions_and_bool_types(self):
        packet = self.blind()
        good = fixture_blind_output(packet)
        for change in (lambda o: o.update(blind_sample_id="CASE_other"), lambda o: o["dimensions"].pop(),
                       lambda o: o["dimensions"].reverse(), lambda o: o["dimensions"][1].update(dimension_id="D01"),
                       lambda o: o["dimensions"][0].update(mastered=1), lambda o: o["dimensions"][0].update(recognized_gap=0),
                       lambda o: o.update(answers=[])):
            out = deepcopy(good)
            change(out)
            self.reject(m.parse_blind_evaluation_output, dumps(out), packet)
        raw = dumps(good)
        for text in ("```json\n" + raw + "\n```", raw.replace('"summary": 1', '"summary": 1, "summary": 1'),
                     raw.replace('"summary": 1', '"summary": NaN')):
            self.reject(m.parse_blind_evaluation_output, text, packet)

    def test_28_quote_exact_individual_note_not_rubric_opposing_or_concat(self):
        packet = self.blind()
        good = fixture_blind_output(packet)
        for quote in (None, "", "不存在的证据", packet["opposing_view"]["summary"], packet["dimensions"][0]["criteria"],
                      packet["evidence_notes"][0]["known_evidence"] + packet["evidence_notes"][0]["remaining_question"]):
            out = deepcopy(good)
            out["dimensions"][0]["evidence_quote"] = quote
            self.reject(m.parse_blind_evaluation_output, dumps(out), packet)
        good["dimensions"][2]["evidence_quote"] = "尚需检查样例适用边界"
        self.assertEqual(self.evaluated(packet, good)["output"]["dimensions"][2]["evidence_quote"], "尚需检查样例适用边界")
        good["dimensions"][4]["evidence_quote"] = "不存在的引文即使双false也拒绝"
        self.reject(m.parse_blind_evaluation_output, dumps(good), packet)

    def test_29_opposing_scores_integer_ranges_rationale_and_leaks(self):
        packet = self.blind()
        for value in (True, False, -1, 3, 1.0, "1"):
            out = fixture_blind_output(packet)
            out["opposing_scores"]["summary"] = value
            self.reject(m.parse_blind_evaluation_output, dumps(out), packet)
        for rationale in ("", "<b>说明</b>", "这是I4处理的T3回答。", "world_id=W01"):
            out = fixture_blind_output(packet)
            out["opposing_scores"]["rationale"] = rationale
            self.reject(m.parse_blind_evaluation_output, dumps(out), packet)

    def test_30_objective_anchor_transfer_denominators_never_mixed(self):
        result = m.score_measurement_record(self.parsed(), self.private())
        self.assertEqual(result["anchor"]["expected_n"], 8)
        self.assertEqual(result["anchor"]["BS"], 0)
        self.assertEqual(result["transfer"]["expected_n"], 4)
        self.assertEqual(result["transfer"]["BS"], 1)
        self.assertNotEqual(result["anchor"]["BS"], 4 / 12)
        self.assertIsNone(result["O"])
        self.assertIsNone(result["U"])
        self.assertIsNone(result["coverage"])
        self.assertEqual(result["blind_evaluation_status"], "pending")
        self.assertFalse(result["formal_eligible"])

    def test_31_missing_answers_bounds_and_transfer_independence(self):
        out = deepcopy(self.out)
        out["answers"] = out["answers"][1:]
        result = m.score_measurement_record(self.parsed(out), self.private())
        a = result["anchor"]
        self.assertIsNone(a["BS"])
        self.assertEqual((a["BS_lower"], a["BS_upper"]), (0, 1 / 8))
        self.assertEqual(a["missing_ids"], ["Q01"])
        self.assertEqual(result["transfer"]["BS"], 1)
        out["answers"] = []
        result = m.score_measurement_record(self.parsed(out), self.private())
        self.assertEqual((result["anchor"]["BS_lower"], result["anchor"]["BS_upper"]), (0, 1))
        self.assertEqual(result["anchor"]["observed_n"], 0)
        self.assertIsNone(result["anchor"]["Acc"])

    def test_32_no_transfer_and_probability_half_choice_convention(self):
        q = fixture_questionnaire(False)
        out = fixture_output(q)
        out["answers"][0].update(p_true=0.5, choice=False)
        result = m.score_measurement_record(self.parsed(out, q), self.private(fixture_gold(q), q))
        self.assertIsNone(result["transfer"])
        self.assertEqual(result["anchor"]["uncertain_count"], 1)
        self.assertEqual(result["anchor"]["choice_inconsistencies"], 1)
        self.assertEqual(result["anchor"]["BS"], 0.25 / 8)

    def test_33_coverage_mastery_priority_and_U_metrics_exact_alignment(self):
        record, gold = self.parsed(), self.private()
        packet = self.blind(record, gold)
        evaluation = self.evaluated(packet)
        with mock.patch.object(metrics, "score_answers", wraps=metrics.score_answers) as objective, \
                mock.patch.object(metrics, "coverage_score", wraps=metrics.coverage_score) as coverage, \
                mock.patch.object(metrics, "understanding_score", wraps=metrics.understanding_score) as understanding:
            result = m.score_measurement_record(record, gold, evaluation)
        self.assertEqual(objective.call_count, 2)
        self.assertEqual([len(call.args[1]) for call in objective.call_args_list], [8, 4])
        coverage.assert_called_once_with(evaluation["output"]["dimensions"])
        understanding.assert_called_once_with([1, 2, 0])
        self.assertEqual(result["coverage"], metrics.coverage_score(evaluation["output"]["dimensions"]))
        self.assertAlmostEqual(result["O"], 100 * 2 / 6)
        self.assertAlmostEqual(result["coverage"]["recognized_gap_rate"], 100 * 2 / 6)
        self.assertEqual(result["U"], 50)
        self.assertEqual(result["blind_evaluation_status"], "validated")

    def test_34_evaluation_other_sample_wrong_rubric_or_mutation_rejected(self):
        record, private = self.parsed(), self.private()
        evaluation = self.evaluated()
        wrong = deepcopy(evaluation)
        wrong["output"]["opposing_scores"]["summary"] = 2
        self.reject(m.score_measurement_record, record, private, wrong)
        out = deepcopy(self.out)
        out["evidence_notes"][0]["known_evidence"] += "另一份样例"
        other_record = self.parsed(out)
        other_packet = self.blind(other_record, private)
        self.reject(m.score_measurement_record, record, private, self.evaluated(other_packet))
        other_gold = deepcopy(self.gold)
        other_gold["dimensions"][0]["criteria"] += "新的量规版本"
        other_private = self.private(other_gold)
        other_packet = self.blind(record, other_private)
        self.reject(m.score_measurement_record, record, private, self.evaluated(other_packet))
        self.reject(m.score_measurement_record, record, private, evaluation["output"])

    def test_35_encoded_html_control_metadata_in_visible_cards(self):
        for content in ("&amp;lt;b&amp;gt;文本&amp;lt;/b&amp;gt;", "文字&#10;", "文字&#8203;"):
            out = deepcopy(self.out)
            out["evidence_notes"][0]["known_evidence"] = content
            self.reject(self.parsed, out)
        for key, value in (("source_url", "https://example.invalid/%2567old/entry"),
                           ("text", "gold_answers: 私有内容"), ("transformation_type", "is_accurate")):
            world = fixture_world()
            world["exposure_history"][0]["cards"][0][key] = value
            self.reject(m.prepare_subject_measurement_context, world, "S01", self.q, allow_demo=True)

    def test_36_public_system_prompt_isolated_and_private_system_rejected(self):
        world = fixture_world()
        world["system_prompt"] = "你是读取公开资料的受试者，仅讨论封闭样例。"
        before = deepcopy(world)
        context = m.prepare_subject_measurement_context(world, "S01", self.q, allow_demo=True)
        self.assertEqual(context["messages"][0], {"role": "system", "content": world["system_prompt"]})
        self.assertEqual(world, before)
        world["system_prompt"] = "gold_answers: 泄露的真值"
        self.reject(m.prepare_subject_measurement_context, world, "S01", self.q, allow_demo=True)

    def test_37_matching_non_demo_flags_not_a_real_observation_claim(self):
        q = deepcopy(self.q)
        q["is_demo"] = False
        world = fixture_world()
        world["is_demo"] = False
        for event in world["exposure_history"]:
            for card in event["cards"]:
                card["is_demo"] = False
        result = m.prepare_subject_measurement_context(world, "S01", q)
        self.assertFalse(result["delivery_claim_verified_externally"] or result["formal_eligible"])
        # 标记变更只测旗标门槛；夹具仍是合成内存，不写成正式观测。
        parsed = self.parsed(q=q)
        self.assertFalse(parsed["actual_observation_verified"])

    def test_38_blind_packet_extra_fields_forbidden_and_input_unchanged(self):
        packet = self.blind()
        out = fixture_blind_output(packet)
        for key in ("answers", "gold_answers", "p_true", "world_id", "is_demo", "timepoint"):
            wrong = deepcopy(packet)
            wrong[key] = []
            self.reject(m.parse_blind_evaluation_output, dumps(out), wrong)
        before = deepcopy(packet)
        self.evaluated(packet)
        self.assertEqual(packet, before)

    def test_39_huge_raw_or_numeric_and_empty_gold_refused(self):
        self.reject(m.parse_subject_measurement_output, " " * 200001, self.q, visible_card_ids=[])
        out = deepcopy(self.out)
        out["answers"][0]["p_true"] = 10 ** 400
        self.reject(self.parsed, out)
        gold = deepcopy(self.gold)
        gold["gold_answers"] = []
        self.reject(self.private, gold)

    def test_40_all_interfaces_no_mutation_or_file_write(self):
        world = fixture_world()
        before = (deepcopy(world), deepcopy(self.q), deepcopy(self.gold), deepcopy(self.out))
        context = m.prepare_subject_measurement_context(world, "S01", self.q, allow_demo=True)
        record = self.parsed(visible=context["visible_card_ids"], point=context["timepoint"])
        private = self.private()
        bridge = m.extract_t1_natural_view(record, self.q)
        packet = self.blind(record, private)
        evaluation = self.evaluated(packet)
        result = m.score_measurement_record(record, private, evaluation)
        self.assertEqual(before, (world, self.q, self.gold, self.out))
        for data in (context, record, private, bridge, packet, evaluation, result):
            self.assertEqual(json.loads(dumps(data)), data)
        self.assertEqual(self.before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.before})

    def test_41_routine_view_order_exact_quotes_no_world_writeback(self):
        world = fixture_routine_world()
        before = deepcopy(world)
        with mock.patch.object(runtime, "measurement_view", wraps=runtime.measurement_view) as copy_state:
            result = m.extract_t1_routine_speech_view(world, "S01", allow_demo=True)
        copy_state.assert_called_once_with(world)
        self.assertEqual(result["contract"], "t1_routine_speech_view_v1")
        self.assertEqual(result["content_origin"], "subject_routine_speech")
        self.assertTrue(result["natural_routine_speech"])
        self.assertFalse(result["requires_protocol_review"] or result["writes_to_memory"])
        self.assertEqual(result["speech_ids"], ["SP01", "SP02"])
        self.assertEqual(result["rounds"], [1, 2])
        self.assertEqual(result["source_event_ids"], ["RE01", "RE02"])
        for entry in world["routine_speech_history"]:
            self.assertIn(entry["text"], result["user_view"])
        self.assertEqual(result["user_view_sha256"], hashlib.sha256(result["user_view"].encode()).hexdigest())
        self.assertEqual(result["world_state_before_sha256"], result["world_state_after_sha256"])
        self.assertFalse(result["actual_observation_verified"] or result["formal_eligible"])
        result["speech_ids"].append("X")
        self.assertEqual(world, before)
        self.assertNotIn("T1", result["user_view"])
        self.assertNotIn("S01", result["user_view"])
        self.assertNotIn("I4", result["user_view"])

    def test_42_routine_filters_other_people_and_topics_without_contamination(self):
        world = fixture_routine_world()
        world["routine_speech_history"] += [fixture_routine_speech(3, agent="S02"), fixture_routine_speech(4, topic="OTHER")]
        result = m.extract_t1_routine_speech_view(world, "S01", topic_id="SYNTHETIC", allow_demo=True)
        self.assertEqual(result["speech_ids"], ["SP01", "SP02"])
        self.assertEqual(len(result["excluded_entries"]), 2)
        self.assertNotIn(world["routine_speech_history"][2]["text"], result["user_view"])
        self.assertNotIn(world["routine_speech_history"][3]["text"], result["user_view"])
        self.reject(m.extract_t1_routine_speech_view, world, "S01", allow_demo=True)
        world["routine_speech_history"] = [fixture_routine_speech(3, agent="S02")]
        self.reject(m.extract_t1_routine_speech_view, world, "S01", allow_demo=True)

    def test_43_routine_latest_whole_entries_are_deterministic_and_audited(self):
        world = fixture_routine_world()
        world["routine_speech_history"] = [fixture_routine_speech(n) for n in reversed(range(1, 9))]
        result = m.extract_t1_routine_speech_view(world, "S01", max_entries=3, allow_demo=True)
        self.assertEqual(result["speech_ids"], ["SP06", "SP07", "SP08"])
        self.assertEqual(result["eligible_entry_count"], 8)
        self.assertEqual(len(result["excluded_entries"]), 5)
        self.assertTrue(all(x["reason"] == "outside_most_recent_entry_limit" for x in result["excluded_entries"]))
        self.assertEqual(result, m.extract_t1_routine_speech_view(world, "S01", max_entries=3, allow_demo=True))
        for value in (0, -1, 65, True, 1.0):
            self.reject(m.extract_t1_routine_speech_view, world, "S01", max_entries=value, allow_demo=True)

    def test_44_routine_measurement_system_placeholder_and_empty_rejected(self):
        changes = [("event_type", "measurement_response"), ("role", "system"), ("role", "user"),
                   ("phase", "measurement"), ("phase", "intervention"), ("generated", False), ("generated", 1),
                   ("status", "prepared"), ("status", "failed"), ("text", ""), ("text", "  ")]
        for key, value in changes:
            world = fixture_routine_world()
            world["routine_speech_history"][0][key] = value
            with self.subTest(key=key, value=value):
                self.reject(m.extract_t1_routine_speech_view, world, "S01", allow_demo=True)
        world = fixture_routine_world()
        world["routine_speech_history"][0]["answers"] = self.out["answers"]
        self.reject(m.extract_t1_routine_speech_view, world, "S01", allow_demo=True)
        world.pop("routine_speech_history")
        world["messages"]["S01"].append({"role": "assistant", "content": "不能由消息回退猜测常规来源。"})
        self.reject(m.extract_t1_routine_speech_view, world, "S01", allow_demo=True)

    def test_45_routine_leak_words_html_and_controls_rejected(self):
        for text in ("我属于I4组。", "本条来自T1。", "S01的观点。", "gold_answers：内容", "真假答案", "p_true为0.5", 
                     dumps(self.out), "系统提示词内容。", "<b>观点</b>", "&lt;b&gt;观点&lt;/b&gt;", "发言\n内容", "发言\u200b内容"):
            world = fixture_routine_world()
            world["routine_speech_history"][0]["text"] = text
            with self.subTest(text=text):
                self.reject(m.extract_t1_routine_speech_view, world, "S01", allow_demo=True)

    def test_46_routine_snapshot_round_demo_and_future_guards(self):
        world = fixture_routine_world()
        self.reject(m.extract_t1_routine_speech_view, world, "S01")
        for key, value in (("timepoint", "T0"), ("timepoint", "T2"), ("round", 7), ("round", True),
                           ("logical_seq", None), ("logical_seq", 5), ("is_demo", 1)):
            bad = deepcopy(world)
            bad[key] = value
            self.reject(m.extract_t1_routine_speech_view, bad, "S01", allow_demo=True)
        for key, value in (("round", 9), ("round", 0), ("logical_seq", 101), ("logical_seq", True),
                           ("world_id", "OTHER"), ("agent_id", "S99"), ("is_demo", False)):
            bad = deepcopy(world)
            bad["routine_speech_history"][0][key] = value
            self.reject(m.extract_t1_routine_speech_view, bad, "S01", allow_demo=True)
        self.reject(m.extract_t1_routine_speech_view, world, "S99", allow_demo=True)

    def test_47_routine_duplicate_records_and_order_conflicts_rejected(self):
        for key in ("speech_id", "event_id", "logical_seq"):
            world = fixture_routine_world()
            world["routine_speech_history"][0][key] = world["routine_speech_history"][1][key]
            self.reject(m.extract_t1_routine_speech_view, world, "S01", allow_demo=True)
        world = fixture_routine_world()
        world["routine_speech_history"][0]["logical_seq"] = 5
        self.reject(m.extract_t1_routine_speech_view, world, "S01", allow_demo=True)

    def test_48_routine_invalid_old_entry_not_hidden_by_limit(self):
        world = fixture_routine_world()
        world["routine_speech_history"][1]["text"] = ""
        self.reject(m.extract_t1_routine_speech_view, world, "S01", max_entries=1, allow_demo=True)

    def test_49_old_measurement_bridge_and_eight_api_prefix_unchanged(self):
        prefix = (ROOT / "cocoon/measurement.py").read_bytes()[:27765]
        self.assertEqual(hashlib.sha256(prefix).hexdigest(), "b13e4e2c2ffcfb072aaa68be839362ebeb3945a4a9f54747026af4e677fa1ed3")
        old = m.extract_t1_natural_view(self.parsed(), self.q)
        new = m.extract_t1_routine_speech_view(fixture_routine_world(), "S01", allow_demo=True)
        self.assertEqual(old["content_origin"], "subject_measurement_extracted")
        self.assertFalse(old["natural_routine_speech"])
        self.assertTrue(old["requires_protocol_review"])
        self.assertNotIn(self.out["answers"][0]["brief_reason"], new["user_view"])

    def test_50_registry_deterministic_shuffle_and_public_payload_only(self):
        entries = fixture_registry_entries()
        before = deepcopy(entries)
        registry = m.create_blind_sample_registry(entries, registry_seed=37)
        self.assertEqual(registry, m.create_blind_sample_registry(entries, registry_seed=37))
        self.assertNotEqual(registry["permutation"], list(range(8)))
        self.assertNotEqual(registry["permutation"], m.create_blind_sample_registry(entries, registry_seed=38)["permutation"])
        self.assertEqual(registry["contract"], "validated_blind_registry_v1")
        self.assertTrue(registry["controller_private_only"])
        packets = registry["public_blind_packets"]
        self.assertEqual([p["blind_sample_id"] for p in packets], [f"BLIND_{i:04d}" for i in range(1, 9)])
        for packet in packets:
            self.assertEqual(set(packet), {"blind_sample_id", "evidence_notes", "opposing_view", "dimensions", "opposing_rubric"})
            m.parse_blind_evaluation_output(dumps(fixture_blind_output(packet)), packet)
        public = dumps(packets)
        for token in ("world_id", "agent_id", "timepoint", "arm", "environment", "block_id", "seed", "S01", "T0", "T1", "T2", "T3", "I4", "I5", "gold_answers", "p_true", "private_mapping"):
            self.assertNotIn('"' + token + '"', public)
        self.assertEqual(entries, before)
        self.assertEqual(registry["private_mapping_sha256"], runtime.canonical_hash(registry["private_mapping"]))
        self.assertEqual(registry["registry_sha256"], runtime.canonical_hash({k: v for k, v in registry.items() if k != "registry_sha256"}))

    def test_51_registry_exact_metadata_and_strong_measurement_binding(self):
        entries = fixture_registry_entries()
        registry = m.create_blind_sample_registry(entries, registry_seed=23)
        for sid, mapping in registry["private_mapping"].items():
            source = entries[mapping["source_entry_index"]]
            for key in ("world_id", "agent_id", "environment", "arm", "timepoint", "block_id"):
                self.assertEqual(mapping[key], source[key])
            self.assertEqual(mapping["measurement_output_sha256"], source["parsed_measurement"]["output_sha256"])
            self.assertEqual(mapping["measurement_record_sha256"], runtime.canonical_hash(source["parsed_measurement"]))
            self.assertEqual(mapping["source_entry_sha256"], runtime.canonical_hash(source))
            packet = next(p for p in registry["public_blind_packets"] if p["blind_sample_id"] == sid)
            self.assertEqual(mapping["blind_packet_sha256"], runtime.canonical_hash(packet))

    def test_52_registry_invalid_seed_empty_duplicates_and_identity(self):
        entries = fixture_registry_entries()
        for seed in (-1, True, 1.0, "42", 2 ** 64):
            self.reject(m.create_blind_sample_registry, entries, registry_seed=seed)
        self.reject(m.create_blind_sample_registry, [], registry_seed=1)
        self.reject(m.create_blind_sample_registry, entries + [deepcopy(entries[0])], registry_seed=1)
        for key, value in (("agent_id", ""), ("world_id", ""), ("block_id", True), ("environment", ""),
                           ("timepoint", "T3"), ("timepoint", None), ("arm", "I9")):
            bad = deepcopy(entries)
            bad[0][key] = value
            self.reject(m.create_blind_sample_registry, bad, registry_seed=1)
        bad = deepcopy(entries)
        bad[0]["extra"] = "not allowed"
        self.reject(m.create_blind_sample_registry, bad, registry_seed=1)
        entries[0]["arm"] = None
        for item in entries[:4]:
            item["arm"] = None
        self.assertEqual(m.create_blind_sample_registry(entries, registry_seed=1)["entry_count"], 8)

    def test_53_registry_world_conflicts_demo_mix_and_gold_mismatch(self):
        for key, value in (("arm", "I0"), ("environment", "A"), ("block_id", "B99")):
            bad = fixture_registry_entries()
            bad[1][key] = value
            self.reject(m.create_blind_sample_registry, bad, registry_seed=1)
        bad = fixture_registry_entries()
        q = fixture_questionnaire()
        q["is_demo"] = False
        out = fixture_output(q)
        bad[1]["parsed_measurement"] = m.parse_subject_measurement_output(dumps(out), q, visible_card_ids=["CARD_01"], timepoint="T1")
        bad[1]["gold_bundle"] = m.validate_private_gold_bundle(fixture_gold(q), q)
        self.reject(m.create_blind_sample_registry, bad, registry_seed=1)
        bad = fixture_registry_entries()
        bad[0]["gold_bundle"]["bundle"]["gold_answers"][0]["truth"] = 1
        self.reject(m.create_blind_sample_registry, bad, registry_seed=1)

    def test_54_registry_public_text_leaks_still_rejected(self):
        for marker in ("I4", "T3", "world_id", "S01"):
            entries = fixture_registry_entries()
            q = entries[0]["parsed_measurement"]["questionnaire"]
            out = deepcopy(entries[0]["parsed_measurement"]["output"])
            out["evidence_notes"][0]["remaining_question"] += " " + marker
            entries[0]["parsed_measurement"] = m.parse_subject_measurement_output(dumps(out), q, visible_card_ids=["CARD_01"], timepoint="T0")
            self.reject(m.create_blind_sample_registry, entries, registry_seed=1)

    def test_55_reconcile_all_scores_and_private_long_panel_consistent(self):
        entries = fixture_registry_entries()
        registry = m.create_blind_sample_registry(entries, registry_seed=37)
        measurements = registry_measurements(registry)
        evaluations = registry_evaluations(registry)
        output = m.reconcile_blind_evaluations(registry, list(reversed(evaluations)), measurements, entries[0]["gold_bundle"])
        self.assertEqual(output["completed_blind_evaluations"], 8)
        self.assertEqual(output["pending_blind_evaluations"], 0)
        self.assertEqual(output["complete_panel_records"], 8)
        self.assertEqual(output["status"], "all_blind_evaluations_reconciled")
        self.assertTrue(output["controller_private_only"])
        self.assertFalse(output["send_to_evaluator"] or output["writes_to_memory"] or output["formal_eligible"])
        by_eval = {e["output"]["blind_sample_id"]: e for e in evaluations}
        for panel in output["panel_records"]:
            sid = panel["blind_sample_id"]
            mapping = registry["private_mapping"][sid]
            expected = m.score_measurement_record(measurements[sid], entries[0]["gold_bundle"], by_eval[sid])
            for key in ("world_id", "agent_id", "environment", "arm", "timepoint", "block_id"):
                self.assertEqual(panel[key], mapping[key])
            for key in ("anchor", "transfer", "O", "U", "coverage"):
                self.assertEqual(panel[key], expected[key])
            self.assertEqual(panel["BS"], expected["anchor"]["BS"])
            self.assertEqual(panel["anchor"]["expected_n"], 8)
            self.assertEqual(panel["transfer"]["expected_n"], 4)
        self.assertEqual(output["panel_sha256"], runtime.canonical_hash(output["panel_records"]))

    def test_56_partial_or_no_evaluation_keeps_all_objective_rows(self):
        entries = fixture_registry_entries()
        registry = m.create_blind_sample_registry(entries, registry_seed=4)
        measurements = registry_measurements(registry)
        evaluations = registry_evaluations(registry)
        for count in (0, 3):
            result = m.reconcile_blind_evaluations(registry, evaluations[:count], measurements, entries[0]["gold_bundle"])
            self.assertEqual(len(result["panel_records"]), 8)
            self.assertEqual(result["pending_blind_evaluations"], 8 - count)
            self.assertEqual(result["complete_panel_records"], count)
            for panel in result["panel_records"][count:]:
                self.assertIsNone(panel["O"])
                self.assertIsNone(panel["U"])
                self.assertEqual(panel["blind_evaluation_status"], "pending")
                self.assertIsNotNone(panel["BS"])

    def test_57_unknown_duplicate_and_raw_unvalidated_evaluations_rejected(self):
        entries = fixture_registry_entries()
        registry = m.create_blind_sample_registry(entries, registry_seed=37)
        measurements = registry_measurements(registry)
        evaluations = registry_evaluations(registry)
        self.reject(m.reconcile_blind_evaluations, registry, [evaluations[0], evaluations[0]], measurements, entries[0]["gold_bundle"])
        other_packet = deepcopy(registry["public_blind_packets"][0])
        other_packet["blind_sample_id"] = "BLIND_9999"
        other = self.evaluated(other_packet)
        self.reject(m.reconcile_blind_evaluations, registry, [other], measurements, entries[0]["gold_bundle"])
        self.reject(m.reconcile_blind_evaluations, registry, [evaluations[0]["output"]], measurements, entries[0]["gold_bundle"])

    def test_58_cross_sample_relabelled_valid_evaluation_is_blocked(self):
        entries = fixture_registry_entries()
        registry = m.create_blind_sample_registry(entries, registry_seed=5)
        measurements = registry_measurements(registry)
        packet = deepcopy(registry["public_blind_packets"][0])
        packet["blind_sample_id"] = registry["public_blind_packets"][1]["blind_sample_id"]
        # 重新过单包契约也不能把另一个样本的notes用于这个匿名ID。
        forged = self.evaluated(packet)
        self.reject(m.reconcile_blind_evaluations, registry, [forged], measurements, entries[0]["gold_bundle"])
        valid = registry_evaluations(registry)[0]
        valid["blind_packet_sha256"] = "0" * 64
        self.reject(m.reconcile_blind_evaluations, registry, [valid], measurements, entries[0]["gold_bundle"])

    def test_59_measurement_set_swaps_and_same_text_objective_change_rejected(self):
        entries = fixture_registry_entries()
        registry = m.create_blind_sample_registry(entries, registry_seed=2)
        records = registry_measurements(registry)
        ids = list(records)
        changes = []
        missing = deepcopy(records)
        missing.pop(ids[0])
        changes.append(missing)
        extra = deepcopy(records)
        extra["BLIND_9999"] = records[ids[0]]
        changes.append(extra)
        swapped = deepcopy(records)
        swapped[ids[0]], swapped[ids[1]] = swapped[ids[1]], swapped[ids[0]]
        changes.append(swapped)
        altered = deepcopy(records)
        original = records[ids[0]]
        out = deepcopy(original["output"])
        out["answers"][0]["p_true"] = 0.77
        altered[ids[0]] = m.parse_subject_measurement_output(dumps(out), original["questionnaire"], visible_card_ids=original["visible_card_ids"], timepoint=original["timepoint"])
        changes.append(altered)
        for changed in changes:
            self.reject(m.reconcile_blind_evaluations, registry, [], changed, entries[0]["gold_bundle"])

    def test_60_registry_hash_and_reconstruction_both_checked(self):
        entries = fixture_registry_entries()
        registry = m.create_blind_sample_registry(entries, registry_seed=37)
        records = registry_measurements(registry)
        changes = [lambda r: r["private_mapping"]["BLIND_0001"].update(arm="I0"),
                   lambda r: r["private_mapping"]["BLIND_0001"].update(world_id="WOTHER"),
                   lambda r: r["permutation"].reverse(), lambda r: r["public_blind_packets"].reverse(),
                   lambda r: r.update(entry_count=1), lambda r: r.update(controller_private_only=False),
                   lambda r: r.update(formal_eligible=True), lambda r: r["source_entries"][0].update(agent_id="S99")]
        for change in changes:
            for rehash in (False, True):
                forged = deepcopy(registry)
                change(forged)
                if rehash:
                    forged["registry_sha256"] = runtime.canonical_hash({k: v for k, v in forged.items() if k != "registry_sha256"})
                self.reject(m.reconcile_blind_evaluations, forged, [], records, entries[0]["gold_bundle"])

    def test_61_registry_multiple_gold_bundles_and_wrong_truth_binding(self):
        entries = fixture_registry_entries(4)
        q = fixture_questionnaire(False)
        q["questionnaire_id"] = "QSET02"
        other = fixture_output(q)
        entries[1]["parsed_measurement"] = self.parsed(other, q=q, point="T1")
        entries[1]["gold_bundle"] = self.private(fixture_gold(q), q=q)
        registry = m.create_blind_sample_registry(entries, registry_seed=11)
        records = registry_measurements(registry)
        golds = {sid: entries[row["source_entry_index"]]["gold_bundle"] for sid, row in registry["private_mapping"].items()}
        result = m.reconcile_blind_evaluations(registry, registry_evaluations(registry), records, golds)
        self.assertEqual(len(result["panel_records"]), 4)
        self.assertEqual(sum(p["transfer"] is None for p in result["panel_records"]), 1)
        self.reject(m.reconcile_blind_evaluations, registry, [], records, entries[0]["gold_bundle"])
        altered = deepcopy(golds)
        sid = list(altered)[0]
        source = altered[sid]
        gold = deepcopy(source["bundle"])
        gold["gold_answers"][0]["truth"] = 1 - gold["gold_answers"][0]["truth"]
        altered[sid] = self.private(gold, q=source["questionnaire"])
        self.reject(m.reconcile_blind_evaluations, registry, [], records, altered)
        missing = deepcopy(golds)
        missing.pop(sid)
        self.reject(m.reconcile_blind_evaluations, registry, [], records, missing)

    def test_62_missing_objective_answer_not_filled_when_blind_complete(self):
        entries = fixture_registry_entries(4)
        old = entries[0]["parsed_measurement"]
        out = deepcopy(old["output"])
        out["answers"].pop(0)
        entries[0]["parsed_measurement"] = self.parsed(out, point="T0")
        registry = m.create_blind_sample_registry(entries, registry_seed=8)
        result = m.reconcile_blind_evaluations(registry, registry_evaluations(registry), registry_measurements(registry), entries[0]["gold_bundle"])
        self.assertEqual(result["complete_objective_records"], 3)
        self.assertEqual(result["complete_panel_records"], 3)
        missing = next(p for p in result["panel_records"] if p["timepoint"] == "T0")
        self.assertIsNone(missing["BS"])
        self.assertIsNotNone(missing["O"])
        self.assertEqual(missing["anchor"]["missing_ids"], ["Q01"])
        self.assertEqual(missing["BS_upper"] - missing["BS_lower"], 1 / 8)

    def test_63_registries_input_and_return_objects_are_independent(self):
        entries = fixture_registry_entries()
        before = deepcopy(entries)
        registry = m.create_blind_sample_registry(entries, registry_seed=14)
        records = registry_measurements(registry)
        evaluations = registry_evaluations(registry)
        snapshots = (deepcopy(registry), deepcopy(records), deepcopy(evaluations))
        result = m.reconcile_blind_evaluations(registry, evaluations, records, entries[0]["gold_bundle"])
        self.assertEqual((registry, records, evaluations), snapshots)
        self.assertEqual(entries, before)
        result["panel_records"][0]["anchor"]["missing_ids"].append("X")
        self.assertEqual((registry, records, evaluations), snapshots)
        registry["private_mapping"]["BLIND_0001"]["world_id"] = "NEW"
        self.assertEqual(entries, before)

    def test_64_full_offline_routine_mirror_extraction_measurement_blinding_panel(self):
        from cocoon import extractive_control as ec
        from cocoon.dosage import RESPONSE_PROMPT
        world = fixture_routine_world()
        original_world = deepcopy(world)
        view = m.extract_t1_routine_speech_view(world, "S01", allow_demo=True)
        texts = [
            "封闭纸环样例说明，每班座位数需要结合实际班次数核对，本句仅为合成实验接口材料，并不描述现实交通运行情况。另一段条件说明，车辆数量、班次安排和能否使用全部座位需要分别记录，不能从其中一个数字替代对完整样例的检查。",
            "合成样例中不同纸站的覆盖条件由规则预先给出，讨论者需要同时说明路段范围和观察区间，而不能默认适用于未说明地点。材料中的适用条件保留为独立文字，尚未核查的部分须承认未知，不把猜测的服务表现写成已有观测或确定承诺。",
            "另一项封闭规则要求维护期间的可用车辆与正常时段分开计算，比较方案时要保留各自条件而非只引用一个方便的总数。这里的数字与条件全部是合成内存夹具，只用于检查完整句引用和管道拼装，没有真实群体认知变化或外部事件依据。",
        ]
        evidence = [{"evidence_id": f"E{i+1:02d}", "text": text, "source_url": "https://example.invalid/r", "source_family_id": f"F{i+1:02d}",
                     "content_sha256": hashlib.sha256(text.encode()).hexdigest()} for i, text in enumerate(texts)]
        packet = {"schema_version": "1.0", "packet_id": "P01", "topic_id": "SYNTHETIC", "topic": "合成纸环接口样例", "is_demo": True,
                  "stance_axis": {"name": "样例选择", "negative_label": "条件甲", "positive_label": "条件乙"}, "evidence": evidence}
        mirror_texts = [
            "常规发言提出了核对具体证据和适用条件的要求，阅读所列样例时可将班次记录与覆盖范围分别理解，避免把不同条件下的说明直接混合。",
            "还可以把已说明的内容和仍需核查的条件分开记录；这种组织方式只针对相同材料，不增加外部数字、事实或关于个体的预设判断。",
            "若后续资料提供了不同条件，应比较其适用范围，而不只比较孤立的结论；目前的引用只追溯到这份封闭样例及原常规发言。",
        ]
        quote = world["routine_speech_history"][1]["text"]
        mirror = {"schema_version": "1.0", "stage": "mirror", "packet_id": "P01",
                  "sections": [{"name": name, "items": []} for name in STAGE_SECTIONS["mirror"]]}
        mirror["sections"][0]["items"] = [{"text": text, "citations": [evidence[i]["evidence_id"]],
                                            "epistemic_status": "evidence_based", "user_quote": quote} for i, text in enumerate(mirror_texts)]
        validated = parse_stage_output(dumps(mirror), "mirror", packet, user_view=view["user_view"])
        organized = "".join(i["text"] + "（" + "、".join(i["citations"]) + "）" for i in validated["output"]["sections"][0]["items"])
        runtime_packet = {"packet_id": "P01", "evidence": [{k: e[k] for k in ("evidence_id", "text", "source_url", "source_family_id")} for e in evidence]}
        sizing = runtime.make_intervention_pair(runtime_packet, "占位", "占位")
        # 只测已存在的纯函数；项目没有build_extractive_control，旧真实运行绑定函数不适用内存夹具。
        fixed = len(sizing["I4"]["evidence_block"]) + 2 + len("；" + RESPONSE_PROMPT)
        selection = ec.select_exact_sentences(evidence, len(organized), fixed, deepcopy(ec._POLICY))
        self.assertEqual(selection["status"], "structurally_matched_candidate")
        plain = "".join(s["text"] + "（" + s["evidence_id"] + "）" for s in selection["selected_sentences"])
        pair = runtime.make_intervention_pair(runtime_packet, organized + "；" + RESPONSE_PROMPT, plain + "；" + RESPONSE_PROMPT)
        self.assertEqual(pair["I4"]["char_count"], fixed + len(organized))
        self.assertEqual(pair["I5"]["char_count"], fixed + len(plain))
        self.assertTrue(pair["checks"]["evidence_equal"])
        self.assertTrue(pair["checks"]["dose_within_range"])
        self.assertTrue(pair["checks"]["length_matched"])
        self.assertLessEqual(abs(len(organized) - len(plain)) / min(len(organized), len(plain)), .1)
        for selected in selection["selected_sentences"]:
            source = next(e for e in evidence if e["evidence_id"] == selected["evidence_id"])
            self.assertEqual(source["text"][selected["start"]:selected["end"]], selected["text"])
        # 两个独立合成分支接收各自候选输入，再从分支建立T2测量副本；不实际调用受试模型。
        branches, entries = [], []
        for arm, world_id in (("I4", "W_BRANCH_A"), ("I5", "W_BRANCH_B")):
            branch = runtime.branch_state(world, world_id)
            branch.update(timepoint="T2", arm=arm)
            # 旧测量历史字段禁止控制符；使用单行JSON阅读信封逐字保留多行候选，不改旧契约。
            reading_message = dumps({"reading_text": pair[arm]["visible_text"]})
            self.assertEqual(json.loads(reading_message)["reading_text"], pair[arm]["visible_text"])
            self.assertTrue(600 <= len(reading_message) <= 800)
            branch["messages"]["S01"].append({"role": "user", "content": reading_message})
            branch_before = deepcopy(branch)
            context = m.prepare_subject_measurement_context(branch, "S01", self.q, allow_demo=True)
            self.assertIn(reading_message, [msg["content"] for msg in context["messages"]])
            other_message = dumps({"reading_text": pair["I5" if arm == "I4" else "I4"]["visible_text"]})
            self.assertNotIn(other_message, [msg["content"] for msg in context["messages"]])
            self.assertLessEqual(abs(len(reading_message) - len(other_message)) / min(len(reading_message), len(other_message)), .1)
            self.assertEqual(branch, branch_before)
            # 回答是预设合成夹具，不声称观察到了处理效应。
            parsed = self.parsed(point=context["timepoint"], visible=context["visible_card_ids"])
            entries.append({"parsed_measurement": parsed, "gold_bundle": self.private(), "world_id": world_id,
                            "agent_id": "S01", "environment": "D", "arm": arm, "timepoint": "T2", "block_id": "B01"})
            branches.append(branch)
        branches[0]["messages"]["S01"][0]["content"] = "仅修改分支甲"
        self.assertNotEqual(branches[0]["messages"]["S01"][0], branches[1]["messages"]["S01"][0])
        registry = m.create_blind_sample_registry(entries, registry_seed=3)
        score = m.reconcile_blind_evaluations(registry, registry_evaluations(registry), registry_measurements(registry),
                                              entries[0]["gold_bundle"], expected_registry_sha256=registry["registry_sha256"])
        self.assertEqual({(p["world_id"], p["arm"], p["timepoint"]) for p in score["panel_records"]},
                         {("W_BRANCH_A", "I4", "T2"), ("W_BRANCH_B", "I5", "T2")})
        self.assertEqual(score["complete_panel_records"], 2)
        for panel in score["panel_records"]:
            self.assertEqual(panel["anchor"]["expected_n"], 8)
            self.assertEqual(panel["transfer"]["expected_n"], 4)
            self.assertEqual(panel["U"], 50)
            self.assertAlmostEqual(panel["O"], 100 * 2 / 6)
        self.assertEqual(world, original_world)
        self.assertTrue(score["is_demo"])
        self.assertFalse(score["formal_eligible"])
        self.assertEqual(score["new_model_calls"], 0)

    def test_65_trusted_registry_digest_blocks_consistent_total_rebuild(self):
        entries = fixture_registry_entries(4)
        registry = m.create_blind_sample_registry(entries, registry_seed=4)
        trusted = registry["registry_sha256"]
        kwargs = dict(expected_registry_sha256=trusted)
        result = m.reconcile_blind_evaluations(registry, [], registry_measurements(registry), entries[0]["gold_bundle"], **kwargs)
        self.assertTrue(result["trusted_registry_digest_supplied"])
        altered = deepcopy(entries)
        for entry in altered:
            entry["world_id"] = "WOTHER"
        rebuilt = m.create_blind_sample_registry(altered, registry_seed=4)
        self.reject(m.reconcile_blind_evaluations, rebuilt, [], registry_measurements(rebuilt), altered[0]["gold_bundle"], **kwargs)
        self.reject(m.reconcile_blind_evaluations, registry, [], registry_measurements(registry), entries[0]["gold_bundle"], expected_registry_sha256="bad")

    def test_66_registry_local_randomness_does_not_mutate_global_rng(self):
        import random
        before = random.getstate()
        m.create_blind_sample_registry(fixture_registry_entries(4), registry_seed=27)
        self.assertEqual(before, random.getstate())


if __name__ == "__main__":
    unittest.main(verbosity=2)
