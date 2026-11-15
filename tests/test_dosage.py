"""剂量接口离线测试；仅在ROOT/tests下mkdtemp，保留测试证据，不清理。"""

from copy import deepcopy
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cocoon import dosage
from cocoon.contracts import validate_packet
from cocoon.dosage import DISPLAY_POLICY, RESPONSE_PROMPT, compact_evidence, make_dose_pair, public_payload
from cocoon.runtime import canonical_hash, make_intervention_pair


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "experiment/evidence_candidates/v1_20261108/build_20261108T074108_f38ec5"
SOURCE_FIELDS = {"source_id", "publisher_short", "publication_date", "source_url", "source_family_id"}
COMPACT_FIELDS = {
    "evidence_block", "evidence_ids", "source_map", "packet_sha256", "policy_sha256",
    "evidence_block_sha256", "char_count",
}
DELIVERY_FIELDS = COMPACT_FIELDS | {
    "additional_text", "additional_text_sha256", "additional_chars", "response_prompt",
    "visible_text", "response_opportunities", "contains_treatment_label", "review_status",
    "formal_eligible",
}
SIX_CHECKS = (
    "evidence_equal", "source_equal", "dose_within_range", "length_matched",
    "additional_length_matched", "responses_equal",
)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def fixture():
    sources = {
        "CL_S01": {"source_id": "CL_S01", "publisher_short": "NOAA",
                   "publication_date": "2025-05-21", "source_url": "https://example.test/dosage/one",
                   "source_family_id": "F_SHARED"},
        "CL_S02": {"source_id": "CL_S02", "publisher_short": "测试机构",
                   "publication_date": None, "source_url": "https://example.test/dosage/two",
                   "source_family_id": "F_SHARED"},
        "CL_S99": {"source_id": "CL_S99", "publisher_short": "未引用机构",
                   "publication_date": "2024-02-29", "source_url": "https://example.test/dosage/unused",
                   "source_family_id": "F_OTHER"},
    }
    evidence = []
    for eid, sid, text in (
        ("CL_E01", "CL_S01", "  保留原文、空白及标点。\n第二行\t<2；e\u0301与\U00020000。  "),
        ("CL_E02", "CL_S02", "另一条完整证据。\n\n内部空行也不删除。\n"),
    ):
        evidence.append({
            "evidence_id": eid, "text": text, "source_url": sources[sid]["source_url"],
            "source_family_id": sources[sid]["source_family_id"], "content_sha256": sha(text),
        })
    packet = {
        "schema_version": "1.0", "packet_id": "PKT_DOSAGE", "topic_id": "T_DOSAGE",
        "topic": "纯离线显示测试", "stance_axis": {"name": "测试轴", "negative_label": "低", "positive_label": "高"},
        "evidence": evidence, "is_demo": False,
    }
    provenance = {"content_origin": "editorial_design_draft", "natural_view_used": False, "source_event_ids": []}
    return packet, sources, provenance


class DosageTests(unittest.TestCase):
    def setUp(self):
        self.packet, self.sources, self.provenance = fixture()

    def pair(self, left="组织段甲", right="组织段乙", **options):
        options.setdefault("provenance", self.provenance)
        return make_dose_pair(self.packet, self.sources, left, right, **options)

    def at_length(self, length):
        # 仅用于精确边界的测试数据；生产接口不具有自动补长能力。
        overhead = self.pair("甲", "乙")["I4"]["char_count"] - 1
        self.assertGreater(length, overhead)
        return "甲" * (length - overhead)

    def resign_outer(self, pair):
        unsigned = deepcopy(pair)
        del unsigned["checks"]["integrity"]["pair_sha256"]
        pair["checks"]["integrity"]["pair_sha256"] = canonical_hash(unsigned)

    def assert_rejected_unchanged(self, pair):
        before = deepcopy(pair)
        with self.assertRaises(ValueError):
            public_payload(pair, "I4")
        self.assertEqual(pair, before)

    def test_policy_stable_json_and_version(self):
        self.assertEqual(DISPLAY_POLICY["version"], "source_id_inline_v1")
        self.assertEqual(json.loads(json_bytes(DISPLAY_POLICY)), DISPLAY_POLICY)
        self.assertEqual(RESPONSE_PROMPT, "请用一段简短文字回应上述材料；如不确定可直接说明。")
        self.assertFalse(DISPLAY_POLICY["extra_browsing_actions"])
        first = compact_evidence(self.packet, self.sources)
        self.assertEqual(first["policy_sha256"], canonical_hash(DISPLAY_POLICY))
        self.assertEqual(first, compact_evidence(self.packet, dict(reversed(list(self.sources.items())))))
        self.assertEqual(set(first), COMPACT_FIELDS)

    def test_policy_mutation_requires_new_version(self):
        pair = self.pair()
        with mock.patch.dict(DISPLAY_POLICY, {"entry_separator": "\n"}):
            with self.assertRaises(ValueError):
                compact_evidence(self.packet, self.sources)
            with self.assertRaises(ValueError):
                public_payload(pair, "I4")
        with mock.patch.object(dosage, "RESPONSE_PROMPT", "改写的提示"):
            with self.assertRaises(ValueError):
                compact_evidence(self.packet, self.sources)

    def test_compact_preserves_utf8_bytes_order_and_internal_whitespace(self):
        before = deepcopy((self.packet, self.sources))
        first, second = self.packet["evidence"]
        expected = ("[CL_E01 | CL_S01 NOAA 2025-05-21]\n" + first["text"] + "\n\n"
                    + "[CL_E02 | CL_S02 测试机构 日期未明]\n" + second["text"])
        compact = compact_evidence(self.packet, self.sources)
        self.assertEqual(compact["evidence_block"].encode("utf-8"), expected.encode("utf-8"))
        self.assertEqual(compact["evidence_ids"], ["CL_E01", "CL_E02"])
        self.assertEqual(compact["evidence_block_sha256"], sha(expected))
        self.assertEqual(compact["packet_sha256"], canonical_hash(self.packet))
        self.assertEqual(compact["char_count"], len(expected))
        self.assertEqual((self.packet, self.sources), before)
        for entry in self.packet["evidence"]:
            self.assertIn(entry["text"].encode("utf-8"), expected.encode("utf-8"))
            self.assertNotIn(entry["source_url"], expected)
            self.assertNotIn(entry["source_family_id"], expected)

    def test_source_map_only_relevant_complete_records_in_first_use_order(self):
        compact = compact_evidence(self.packet, self.sources)
        self.assertEqual(compact["source_map"], [self.sources["CL_S01"], self.sources["CL_S02"]])
        self.assertTrue(all(set(row) == SOURCE_FIELDS for row in compact["source_map"]))
        self.assertIsNone(compact["source_map"][1]["publication_date"])
        self.packet["evidence"].reverse()
        compact = compact_evidence(self.packet, self.sources)
        self.assertEqual([row["source_id"] for row in compact["source_map"]], ["CL_S02", "CL_S01"])

    def test_two_evidence_items_can_share_one_source(self):
        self.packet["evidence"][1]["source_url"] = self.sources["CL_S01"]["source_url"]
        compact = compact_evidence(self.packet, self.sources)
        self.assertEqual(len(compact["source_map"]), 1)
        self.assertEqual(compact["evidence_block"].count("CL_S01 NOAA 2025-05-21"), 2)
        self.assertEqual(len(compact["evidence_ids"]), 2)

    def test_same_url_different_family_is_not_ambiguous(self):
        self.sources["CL_S02"]["source_url"] = self.sources["CL_S01"]["source_url"]
        self.sources["CL_S02"]["source_family_id"] = "F_SECOND"
        self.packet["evidence"][1].update(
            source_url=self.sources["CL_S02"]["source_url"], source_family_id="F_SECOND",
        )
        self.assertEqual(len(compact_evidence(self.packet, self.sources)["source_map"]), 2)

    def test_missing_url_or_family_match_is_rejected_without_modifying_inputs(self):
        for field, value in (("source_url", "https://example.test/dosage/changed"),
                             ("source_family_id", "F_CHANGED")):
            for target in ("packet", "index"):
                packet, sources = deepcopy(self.packet), deepcopy(self.sources)
                row = packet["evidence"][0] if target == "packet" else sources["CL_S01"]
                row[field] = value
                before = deepcopy((packet, sources))
                with self.subTest(field=field, target=target), self.assertRaises(ValueError):
                    compact_evidence(packet, sources)
                self.assertEqual((packet, sources), before)
        missing = deepcopy(self.sources)
        del missing["CL_S01"]
        with self.assertRaises(ValueError):
            compact_evidence(self.packet, missing)

    def test_duplicate_origin_mapping_rejected_even_with_different_source_id(self):
        self.sources["CL_DUP"] = dict(self.sources["CL_S01"], source_id="CL_DUP")
        with self.assertRaisesRegex(ValueError, "歧义"):
            compact_evidence(self.packet, self.sources)

    def test_source_index_and_records_exact_schema(self):
        for invalid in (None, [], {"bad": None}, {1: self.sources["CL_S01"]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                compact_evidence(self.packet, invalid)
        for field in SOURCE_FIELDS:
            sources = deepcopy(self.sources)
            del sources["CL_S01"][field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                compact_evidence(self.packet, sources)
        for extra in ("review", "publisher", "retrieved_at", "treatment_label"):
            sources = deepcopy(self.sources)
            sources["CL_S01"][extra] = "额外字段"
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                compact_evidence(self.packet, sources)
        self.sources["CL_S01"]["source_id"] = "CL_OTHER"
        with self.assertRaises(ValueError):
            compact_evidence(self.packet, self.sources)

    def test_dates_strict_iso_calendar_not_basic_week_or_retrieval_date(self):
        for value in ("2025-02-29", "2024-04-31", "0000-01-01", "2025-5-21", "20250521",
                      "2025-W21-3", "2025-05-21T00:00:00", " 2025-05-21", "2025-05-21\n",
                      "２０２５-０５-２１", "日期未明", "", True, 20250521, date(2025, 5, 21)):
            sources = deepcopy(self.sources)
            sources["CL_S01"]["publication_date"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                compact_evidence(self.packet, sources)
        for value in (None, "2024-02-29", "0001-01-01", "9999-12-31"):
            self.sources["CL_S01"]["publication_date"] = value
            result = compact_evidence(self.packet, self.sources)
            self.assertIn(value or "日期未明", result["evidence_block"])
            self.assertEqual(result["source_map"][0]["publication_date"], value)

    def test_source_labels_ascii_or_chinese_single_line_with_18_char_limit(self):
        for field, invalid in (
            ("source_id", ["<b>S</b>", "S\n01", "S\r01", "S\t01", "S|01", "S]01", "S\x00", "S\ud800", "Sα", "S" * 65]),
            ("publisher_short", ["", " ", "<b>机构</b>", "机构\n新行", "机构\r新行", "机构\t", "机\x00构",
                                 "机\ud800构", "机构\u2028换行", "机构\u202e", "机构[伪标签]", "机构|标签", "é", "机" * 19]),
        ):
            for value in invalid:
                sources = deepcopy(self.sources)
                sources["CL_S01"][field] = value
                if field == "source_id":
                    sources[value] = sources.pop("CL_S01")
                with self.subTest(field=field, value=repr(value)), self.assertRaises(ValueError):
                    compact_evidence(self.packet, sources)
        for label in ("机" * 18, "ASCII Institute", "中文机构（试验）", "NASA/NOAA"):
            self.sources["CL_S01"]["publisher_short"] = label
            self.assertIn(label, compact_evidence(self.packet, self.sources)["evidence_block"])
        self.sources["来源一"] = dict(self.sources.pop("CL_S01"), source_id="来源一")
        self.assertIn("来源一", compact_evidence(self.packet, self.sources)["evidence_block"])

    def test_invalid_url_and_family_rejected_in_entire_index(self):
        for field, value in (("source_url", "http://example.test/dosage/insecure"),
                             ("source_url", "https://example.test/dosage/one#fragment"),
                             ("source_url", "https://example.test/dosage/one?token=secret"),
                             ("source_family_id", "bad\nfamily")):
            sources = deepcopy(self.sources)
            sources["CL_S99"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                compact_evidence(self.packet, sources)

    def test_invalid_packet_or_content_hash_rejected_without_repair(self):
        for field, value in (("content_sha256", "0" * 64), ("text", "已改变但没有更新哈希")):
            packet = deepcopy(self.packet)
            packet["evidence"][0][field] = value
            before = deepcopy(packet)
            with self.subTest(field=field), self.assertRaises(ValueError):
                compact_evidence(packet, self.sources)
            self.assertEqual(packet, before)
        for change in ({"extra": True}, {"is_demo": True}, {"evidence": []}):
            packet = dict(self.packet, **change)
            with self.assertRaises(ValueError):
                compact_evidence(packet, self.sources)

    def test_invalid_text_rejected_in_evidence_and_both_additional_arms(self):
        bad = (None, 2, "", " \n\t", "正文\x00", "正文\r\n后文", "正文\ud800",
               "<b>正文</b>", "正文<!--注释-->", "<!DOCTYPE html>正文", "<?test?>正文",
               "<script", "<img src=x", "正文\x1b")
        for text in bad:
            for arm in ("I4", "I5"):
                left, right = (text, "正常") if arm == "I4" else ("正常", text)
                with self.subTest(arm=arm, text=repr(text)), self.assertRaises(ValueError):
                    self.pair(left, right)
            if type(text) is str and "\ud800" not in text:
                packet = deepcopy(self.packet)
                packet["evidence"][0].update(text=text, content_sha256=sha(text))
                with self.subTest(evidence=repr(text)), self.assertRaises(ValueError):
                    compact_evidence(packet, self.sources)

    def test_exact_visible_assembly_and_total_actual_message_count(self):
        addon = " \n原文说明\t<2；e\u0301\U00020000。\n\n结尾空白  "
        pair = self.pair(addon, addon)
        self.assertEqual(set(pair), {"I4", "I5", "checks", "provenance"})
        for arm in ("I4", "I5"):
            delivery = pair[arm]
            self.assertEqual(set(delivery), DELIVERY_FIELDS)
            expected = ("阅读材料\n" + delivery["evidence_block"] + "\n\n阅读说明\n"
                        + addon + "\n\n" + RESPONSE_PROMPT)
            self.assertEqual(delivery["visible_text"].encode("utf-8"), expected.encode("utf-8"))
            self.assertEqual(delivery["additional_text"], addon)
            self.assertEqual(delivery["additional_chars"], len(addon))
            self.assertEqual(delivery["additional_text_sha256"], sha(addon))
            self.assertEqual(delivery["evidence_block_sha256"], sha(delivery["evidence_block"]))
            self.assertEqual(delivery["char_count"], len(expected))
            messages = public_payload(pair, arm)["messages"]
            self.assertEqual(delivery["char_count"], sum(len(message["content"]) for message in messages))
            self.assertEqual(delivery["response_opportunities"], 1)
        self.assertEqual(pair["checks"]["length_basis"],
                         "Python len includes all Unicode codepoints punctuation whitespace and every model-visible label")

    def test_extra_visible_url_and_labels_are_counted_not_discounted(self):
        text = "新增说明\n附加标识：" + self.sources["CL_S01"]["source_url"]
        delivery = self.pair(text, text)["I4"]
        self.assertEqual(delivery["additional_chars"], len(text))
        self.assertEqual(delivery["char_count"], len(delivery["visible_text"]))
        self.assertIn(text, delivery["visible_text"])

    def test_range_boundaries_600_and_800_inclusive_for_both_arms(self):
        for count in (599, 600, 800, 801):
            pair = self.pair(self.at_length(count), self.at_length(count))
            with self.subTest(count=count):
                self.assertEqual(pair["I4"]["char_count"], count)
                self.assertEqual(pair["I5"]["char_count"], count)
                self.assertEqual(pair["checks"]["dose_within_range"], 600 <= count <= 800)
                self.assertEqual(pair["checks"]["assembly_valid"], 600 <= count <= 800)
                self.assertEqual(len(pair["checks"]["deviations"]), 0 if 600 <= count <= 800 else 2)

    def test_total_ratio_ten_percent_inclusive_and_symmetric(self):
        for left, right in ((700, 770), (770, 700), (700, 771), (771, 700)):
            pair = self.pair(self.at_length(left), self.at_length(right))
            with self.subTest(left=left, right=right):
                self.assertEqual(pair["checks"]["total_difference_ratio"], abs(left - right) / min(left, right))
                self.assertEqual(pair["checks"]["length_matched"], max(left, right) == 770)
                self.assertTrue(pair["checks"]["dose_within_range"])
                # 共有证据不能稀释附加段差异；总长恰好10%时附加段仍可能不匹配。
                self.assertFalse(pair["checks"]["additional_length_matched"])
                self.assertFalse(pair["checks"]["assembly_valid"])

    def test_additional_ratio_separate_ten_percent_boundary(self):
        for left, right in ((400, 440), (440, 400), (400, 441), (441, 400)):
            pair = self.pair("甲" * left, "乙" * right, min_chars=0, max_chars=1000)
            with self.subTest(left=left, right=right):
                self.assertEqual(pair["checks"]["additional_difference_ratio"], abs(left - right) / min(left, right))
                self.assertEqual(pair["checks"]["additional_length_matched"], max(left, right) == 440)
                self.assertTrue(pair["checks"]["length_matched"])
                self.assertEqual(pair["checks"]["assembly_valid"], max(left, right) == 440)
                codes = [entry["code"] for entry in pair["checks"]["deviations"]]
                self.assertEqual("additional_length_mismatch" in codes, max(left, right) == 441)

    def test_assembly_is_exact_conjunction_of_six_checks(self):
        for left, right in (("甲", "乙"), (self.at_length(600), self.at_length(600)),
                            (self.at_length(600), self.at_length(800)), ("甲", "乙" * 1000)):
            checks = self.pair(left, right)["checks"]
            self.assertEqual(checks["assembly_valid"], all(checks[name] for name in SIX_CHECKS))
            self.assertFalse(checks["formal_eligible"])
            self.assertEqual(checks["semantic_review"], "pending_independent_review")

    def test_invalid_numeric_bounds_and_tolerance(self):
        for options in (
            {"min_chars": True}, {"min_chars": 600.0}, {"min_chars": -1}, {"min_chars": None},
            {"max_chars": False}, {"max_chars": "800"}, {"max_chars": math.inf},
            {"max_chars": math.nan}, {"min_chars": 801, "max_chars": 800},
            {"tolerance": True}, {"tolerance": None}, {"tolerance": "0.1"},
            {"tolerance": math.nan}, {"tolerance": math.inf}, {"tolerance": -math.inf},
            {"tolerance": -.1}, {"tolerance": 1.1}, {"tolerance": 10 ** 1000},
        ):
            with self.subTest(options=options), self.assertRaises((ValueError, TypeError)):
                self.pair(**options)
        self.assertTrue(self.pair(tolerance=0)["checks"]["length_matched"])
        self.assertTrue(self.pair("甲", "乙乙", tolerance=1)["checks"]["additional_length_matched"])
        self.assertFalse(self.pair(min_chars=0, max_chars=0)["checks"]["dose_within_range"])
        self.assertTrue(self.pair(min_chars=0, max_chars=10 ** 1000)["checks"]["dose_within_range"])

    def test_no_padding_truncation_repetition_or_evidence_drop(self):
        short, long = "短说明", "完整的长说明" * 500
        pair = self.pair(short, long)
        self.assertFalse(pair["checks"]["dose_within_range"])
        self.assertFalse(pair["checks"]["length_matched"])
        self.assertEqual(pair["I4"]["additional_text"], short)
        self.assertEqual(pair["I5"]["additional_text"], long)
        self.assertEqual(pair["I4"]["evidence_block"], pair["I5"]["evidence_block"])
        for arm, text in (("I4", short), ("I5", long)):
            self.assertEqual(pair[arm]["visible_text"], "阅读材料\n" + pair[arm]["evidence_block"]
                             + "\n\n阅读说明\n" + text + "\n\n" + RESPONSE_PROMPT)
            self.assertFalse(public_payload(pair, arm)["experimental_ready"])
        self.packet["evidence"][0].update(text=long, content_sha256=sha(long))
        pair = self.pair()
        self.assertFalse(pair["checks"]["dose_within_range"])
        self.assertEqual(pair["I4"]["evidence_ids"], ["CL_E01", "CL_E02"])
        self.assertIn(long, pair["I4"]["evidence_block"])

    def test_provenance_required_exact_and_editorial_restrictions(self):
        with self.assertRaises(TypeError):
            make_dose_pair(self.packet, self.sources, "甲", "乙")
        invalid = [None, {}, dict(self.provenance, content_origin="other"),
                   dict(self.provenance, natural_view_used=1), dict(self.provenance, natural_view_used=True),
                   dict(self.provenance, source_event_ids=["event-1"]), dict(self.provenance, source_event_ids=()),
                   dict(self.provenance, review_status="approved")]
        for field in self.provenance:
            missing = deepcopy(self.provenance)
            del missing[field]
            invalid.append(missing)
        for provenance in invalid:
            with self.subTest(provenance=provenance), self.assertRaises(ValueError):
                self.pair(provenance=provenance)

    def test_recorded_events_not_automatically_verified_or_approved(self):
        for natural in (False, True):
            provenance = {"content_origin": "recorded_assistant_output", "natural_view_used": natural,
                          "source_event_ids": ["event-not-verified-by-this-interface"]}
            pair = self.pair(self.at_length(700), self.at_length(700), provenance=provenance)
            self.assertTrue(pair["checks"]["assembly_valid"])
            self.assertIs(pair["checks"]["natural_view_available"], natural)
            self.assertEqual(pair["provenance"], provenance)
            self.assertFalse(pair["checks"]["formal_eligible"])
            for arm in ("I4", "I5"):
                self.assertEqual(pair[arm]["review_status"], "pending")
                self.assertFalse(pair[arm]["formal_eligible"])
                self.assertFalse(public_payload(pair, arm)["experimental_ready"])
        for ids in ([], [""], ["  "], [None], [1], "event", ["bad\x00"], ["bad\ud800"]):
            provenance = {"content_origin": "recorded_assistant_output", "natural_view_used": True,
                          "source_event_ids": ids}
            with self.subTest(ids=repr(ids)), self.assertRaises(ValueError):
                self.pair(provenance=provenance)

    def test_no_arm_labels_inserted_or_audit_metadata_leaked(self):
        pair = self.pair()
        for arm in ("I4", "I5"):
            payload = public_payload(pair, arm)
            self.assertEqual(set(payload), {"messages", "source_map_hash", "payload_hash", "experimental_ready"})
            self.assertEqual(payload["messages"], [{"role": "user", "content": pair[arm]["visible_text"]}])
            self.assertEqual(pair[arm]["visible_text"].count(RESPONSE_PROMPT), 1)
            self.assertFalse(pair[arm]["contains_treatment_label"])
            serialized = json.dumps(payload["messages"], ensure_ascii=False)
            for token in ("I4", "I5", "provenance", "review_status", "semantic_review", "editorial_design_draft",
                          "source_map", "experimental_ready", "formal_eligible", "浏览", "https://"):
                self.assertNotIn(token, serialized)
            self.assertEqual(payload["source_map_hash"], canonical_hash(pair[arm]["source_map"]))
            unsigned = {key: value for key, value in payload.items() if key != "payload_hash"}
            self.assertEqual(payload["payload_hash"], canonical_hash(unsigned))

    def test_input_arm_labels_rejected_not_stripped(self):
        for token in ("I4", "I5", "i4", "i5"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.pair("这是" + token + "组说明", "另一段")
        text = "原始证据带I4标记"
        self.packet["evidence"][0].update(text=text, content_sha256=sha(text))
        before = deepcopy(self.packet)
        with self.assertRaises(ValueError):
            self.pair()
        self.assertEqual(self.packet, before)

    def test_no_mutable_objects_shared_between_inputs_arms_calls_or_exports(self):
        pair = self.pair()
        unchanged = deepcopy(pair)
        other = self.pair()
        compact = compact_evidence(self.packet, self.sources)
        exported = public_payload(pair, "I4")
        self.packet["evidence"][0]["text"] = "外部已修改"
        self.sources["CL_S01"]["publisher_short"] = "外部新机构"
        self.provenance["source_event_ids"].append("external")
        compact["source_map"][0]["source_url"] = "外部新链接"
        compact["evidence_ids"].append("external")
        exported["messages"][0]["content"] = "改写返回消息"
        exported["messages"].append({"role": "user", "content": "额外消息"})
        self.assertEqual(pair, unchanged)
        pair["I4"]["source_map"][0]["publisher_short"] = "仅第一臂"
        pair["I4"]["evidence_ids"].append("changed")
        pair["checks"]["integrity"]["packet"]["evidence"][0]["text"] = "仅审计快照"
        pair["provenance"]["source_event_ids"].append("changed")
        self.assertEqual(pair["I5"], unchanged["I5"])
        self.assertEqual(other, unchanged)

    def test_public_export_deterministic_json_round_trip(self):
        pair = self.pair(self.at_length(700), self.at_length(700))
        before = deepcopy(pair)
        decoded = json.loads(json_bytes(pair))
        self.assertEqual(pair, self.pair(self.at_length(700), self.at_length(700)))
        self.assertEqual(public_payload(pair, "I4"), public_payload(decoded, "I4"))
        self.assertEqual(pair, before)
        for arm in (None, 4, True, "i4", "I6", ""):
            with self.subTest(arm=arm), self.assertRaises(ValueError):
                public_payload(pair, arm)

    def test_all_delivery_hashes_both_arms_rechecked_even_after_outer_resign(self):
        original = self.pair()
        for arm in ("I4", "I5"):
            for field in ("packet_sha256", "policy_sha256", "evidence_block_sha256", "additional_text_sha256"):
                for resign in (False, True):
                    pair = deepcopy(original)
                    pair[arm][field] = "0" * 64
                    if resign:
                        self.resign_outer(pair)
                    with self.subTest(arm=arm, field=field, resign=resign):
                        self.assert_rejected_unchanged(pair)
        pair = deepcopy(original)
        pair["checks"]["integrity"]["pair_sha256"] = "0" * 64
        self.assert_rejected_unchanged(pair)

    def test_dates_urls_and_families_in_either_map_cannot_be_changed(self):
        original = self.pair()
        for field, value in (("publication_date", "2025-05-22"),
                             ("source_url", "https://example.test/dosage/changed"),
                             ("source_family_id", "F_CHANGED"), ("publisher_short", "变更机构")):
            for arms in (("I4",), ("I5",), ("I4", "I5")):
                pair = deepcopy(original)
                for arm in arms:
                    pair[arm]["source_map"][0][field] = value
                with self.subTest(field=field, arms=arms):
                    self.assert_rejected_unchanged(pair)
                    self.resign_outer(pair)
                    self.assert_rejected_unchanged(pair)

    def test_source_file_snapshot_and_bad_hash_failures_do_not_write_or_repair(self):
        root = Path(tempfile.mkdtemp(dir=ROOT / "tests"))
        sourcefile, pairfile = root / "source.json", root / "pair.json"
        sourcefile.write_bytes(json_bytes(self.sources))
        pair = make_dose_pair(self.packet, json.loads(sourcefile.read_bytes()), "甲", "乙", provenance=self.provenance)
        pairfile.write_bytes(json_bytes(pair))
        for field, value in (("publication_date", "2025-02-30"),
                             ("source_url", "https://example.test/dosage/changed"),
                             ("source_family_id", "F_CHANGED")):
            sources = deepcopy(self.sources)
            sources["CL_S01"][field] = value
            sourcefile.write_bytes(json_bytes(sources))
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            with self.subTest(field=field), self.assertRaises(ValueError):
                make_dose_pair(self.packet, json.loads(sourcefile.read_bytes()), "甲", "乙", provenance=self.provenance)
            self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)
            # 已冻结的pair不共享文件内容，也不声称自动监控或验证后来改变的sourcefile。
            self.assertEqual(public_payload(pair, "I4"), public_payload(json.loads(pairfile.read_bytes()), "I4"))
        bad_pair = deepcopy(pair)
        bad_pair["I5"]["additional_text_sha256"] = "0" * 64
        pairfile.write_bytes(json_bytes(bad_pair))
        before = {path.name: path.read_bytes() for path in root.iterdir()}
        self.assert_rejected_unchanged(json.loads(pairfile.read_bytes()))
        self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)

    def test_derived_fields_schema_and_flags_recomputed_not_trusted(self):
        original = self.pair()
        changes = (
            ("visible_text", "伪造可见内容"), ("char_count", 700), ("additional_chars", 1),
            ("evidence_block", "伪造证据"), ("response_prompt", "额外回复一次"),
            ("response_opportunities", 2), ("response_opportunities", True),
            ("contains_treatment_label", True), ("formal_eligible", True), ("review_status", "approved"),
            ("evidence_ids", ["CL_E02", "CL_E01"]),
        )
        for arm in ("I4", "I5"):
            for field, value in changes:
                pair = deepcopy(original)
                pair[arm][field] = value
                self.resign_outer(pair)
                with self.subTest(arm=arm, field=field):
                    self.assert_rejected_unchanged(pair)
        for name in SIX_CHECKS + ("assembly_valid", "natural_view_available", "formal_eligible"):
            pair = deepcopy(original)
            pair["checks"][name] = not pair["checks"][name]
            self.resign_outer(pair)
            self.assert_rejected_unchanged(pair)
        pair = deepcopy(original)
        pair["I4"]["review"] = "额外字段"
        self.resign_outer(pair)
        self.assert_rejected_unchanged(pair)
        pair = deepcopy(original)
        del pair["I5"]["additional_text"]
        self.resign_outer(pair)
        self.assert_rejected_unchanged(pair)

    def test_duplicate_map_and_packet_tampering_rejected_after_outer_resign(self):
        pair = self.pair()
        pair["I4"]["source_map"].append(deepcopy(pair["I4"]["source_map"][0]))
        pair["checks"]["integrity"]["source_map_sha256"] = canonical_hash(pair["I4"]["source_map"])
        self.resign_outer(pair)
        self.assert_rejected_unchanged(pair)
        pair = self.pair()
        pair["checks"]["integrity"]["packet"]["evidence"][0]["text"] = "改证据但不改正文哈希"
        self.resign_outer(pair)
        self.assert_rejected_unchanged(pair)

    def test_legacy_runtime_remains_full_url_and_full_family(self):
        legacy_packet = {"packet_id": self.packet["packet_id"], "evidence": [
            {key: value for key, value in row.items() if key != "content_sha256"}
            for row in self.packet["evidence"]
        ]}
        old = make_intervention_pair(legacy_packet, "甲", "乙")
        new = self.pair("甲", "乙")
        for row in self.packet["evidence"]:
            self.assertIn(row["source_url"], old["I4"]["visible_text"])
            self.assertIn(row["source_family_id"], old["I4"]["visible_text"])
            self.assertNotIn(row["source_url"], new["I4"]["visible_text"])
        self.assertNotEqual(old["I4"]["visible_text"], new["I4"]["visible_text"])
        self.assertEqual(old["I4"]["char_count"], len(old["I4"]["visible_text"]))


@unittest.skipUnless(BUILD.is_dir(), "本机候选证据包不存在；核心接口测试不依赖该目录")
class ExistingCandidateTests(unittest.TestCase):
    def test_six_existing_rounds_preserve_all_text_and_frozen_files(self):
        manifest = json.loads((BUILD / "manifest.json").read_bytes())
        before = {row["path"]: hashlib.sha256((BUILD / row["path"]).read_bytes()).hexdigest()
                  for row in manifest["files"]}
        self.assertEqual(before, {row["path"]: row["sha256"] for row in manifest["files"]})
        records = json.loads((BUILD / "review/sources.json").read_bytes())["sources"]
        # 仅测试显式选择的机构短名；不截断机构名、不从获取时间推断文献日期。
        names = {"CL_S01": "NOAA", "CL_S02": "NASA", "CL_S03": "NOAA", "CL_S04": "NASA",
                 "EN_S02": "IEA", "EN_S05": "ILO/IRENA", "EN_S06": "Nature Commun.",
                 "EN_S07": "pv magazine", "EN_S08": "北京大学能源研究院",
                 "WE_S01": "中国气象局", "WE_S02": "中国气象局", "WE_S04": "中国气象报社", "WE_S06": "中国气象局"}
        records = records.values() if type(records) is dict else records
        index = {record["source_id"]: {"source_id": record["source_id"], "publisher_short": names[record["source_id"]],
                                      "publication_date": record["publication_date"], "source_url": record["url"],
                                      "source_family_id": record["source_family_id"]} for record in records}
        old_lengths, new_lengths = [], []
        for topic in manifest["topics"]:
            for field in ("round_1_packet", "round_2_packet"):
                packet = validate_packet(json.loads((BUILD / topic[field]).read_bytes()))
                compact = compact_evidence(packet, index)
                legacy = {"packet_id": packet["packet_id"], "evidence": [
                    {key: value for key, value in row.items() if key != "content_sha256"} for row in packet["evidence"]]}
                old = make_intervention_pair(legacy, "仅作长度测试", "仅作长度测试")
                old_lengths.append(len(old["I4"]["evidence_block"]))
                new_lengths.append(compact["char_count"])
                self.assertEqual(len(compact["evidence_ids"]), 3)
                for entry in packet["evidence"]:
                    self.assertIn(entry["text"].encode("utf-8"), compact["evidence_block"].encode("utf-8"))
                self.assertLess(compact["char_count"], 600)
        self.assertEqual(len(new_lengths), 6)
        # 当前冻结包天气第一轮实测为725；不把旧版含URL输入声称成新版短输入。
        self.assertEqual(old_lengths, [729, 835, 794, 759, 725, 767])
        self.assertEqual(new_lengths, [412, 412, 456, 438, 468, 510])
        self.assertEqual(before, {row["path"]: hashlib.sha256((BUILD / row["path"]).read_bytes()).hexdigest()
                                  for row in manifest["files"]})


if __name__ == "__main__":
    unittest.main()
