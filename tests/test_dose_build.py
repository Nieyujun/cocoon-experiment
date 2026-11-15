"""剂量构建离线回归；仅写 tests 下自建副本，保留全部临时产物。"""

from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from cocoon.dose_build import create_dose_build, validate_drafts, verify_dose_build
from cocoon.dosage import DISPLAY_POLICY, RESPONSE_PROMPT, compact_evidence, public_payload
from cocoon.runtime import canonical_hash


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_EVIDENCE_BUILD = (
    ROOT / "experiment/evidence_candidates/v1_20261108/build_20261108T074108_f38ec5"
)
DRAFTS = ROOT / "configs/dose_additions_v1.json"
SEMANTIC_REVIEW = ROOT / "experiment/dose_candidates/semantic_review_v1.json"
ARMS = ("I4", "I5")


def read_json(path):
    return json.loads(Path(path).read_bytes())


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hashes(root):
    return {path.relative_to(root).as_posix(): file_hash(path)
            for path in root.rglob("*") if path.is_file()}


class DoseBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_hashes = tree_hashes(ORIGINAL_EVIDENCE_BUILD)
        cls.protected_hashes = {
            path: file_hash(path) for path in (
                DRAFTS, SEMANTIC_REVIEW, ROOT / "cocoon/dose_build.py",
                ROOT / "cocoon/dosage.py",
            )
        }
        cls.original_manifest = read_json(ORIGINAL_EVIDENCE_BUILD / "manifest.json")
        cls.packets = {
            (topic["topic_id"], round_no): read_json(
                ORIGINAL_EVIDENCE_BUILD / topic[f"round_{round_no}_packet"]
            )
            for topic in cls.original_manifest["topics"] for round_no in (1, 2)
        }
        cls.root = Path(tempfile.mkdtemp(prefix="dose-build-base-", dir=ROOT / "tests"))
        parent = cls.root / "experiment/dose_candidates"
        parent.mkdir(parents=True)
        shutil.copy2(SEMANTIC_REVIEW, parent / "semantic_review_v1.json")
        cls.result = create_dose_build(cls.root, ORIGINAL_EVIDENCE_BUILD, DRAFTS)
        cls.build = Path(cls.result["build_directory"])
        cls.manifest = read_json(cls.build / "manifest.json")
        cls.source_index = read_json(cls.build / "inputs/source_index.json")
        cls.drafts = read_json(DRAFTS)
        cls.baseline_hashes = tree_hashes(cls.build)

    @classmethod
    def tearDownClass(cls):
        check = unittest.TestCase()
        check.assertEqual(tree_hashes(ORIGINAL_EVIDENCE_BUILD), cls.original_hashes)
        check.assertEqual({path: file_hash(path) for path in cls.protected_hashes},
                          cls.protected_hashes)
        check.assertEqual(tree_hashes(cls.build), cls.baseline_hashes)
        check.assertEqual(file_hash(cls.root / "experiment/dose_candidates/semantic_review_v1.json"),
                          cls.protected_hashes[SEMANTIC_REVIEW])

    def copy_build(self):
        self.mutation_root = Path(tempfile.mkdtemp(prefix="dose-build-mutation-", dir=ROOT / "tests"))
        out = self.mutation_root / "build"
        shutil.copytree(self.build, out)
        self.assertEqual(tree_hashes(out), self.baseline_hashes)
        return out, read_json(out / "manifest.json")

    def write_copy_json(self, path, value):
        path = Path(path).resolve()
        self.assertTrue(path.is_relative_to(self.mutation_root.resolve()))
        self.assertTrue(path.parent.is_dir())
        path.write_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8"))

    def rewrite_frozen_json(self, out, manifest, relative, value):
        path = out / relative
        self.write_copy_json(path, value)
        records = [row for row in manifest["files"] if row["path"] == relative]
        self.assertEqual(len(records), 1)
        records[0].update(sha256=file_hash(path), bytes=path.stat().st_size)

    def assert_build_rejected(self, out, manifest, reason):
        self.write_copy_json(out / "manifest.json", manifest)
        before = tree_hashes(out)
        with self.assertRaisesRegex(ValueError, reason):
            verify_dose_build(out)
        self.assertEqual(tree_hashes(out), before)

    def assert_drafts_rejected(self, out, manifest, drafts, reason):
        self.rewrite_frozen_json(out, manifest, "inputs/additions.json", drafts)
        self.write_copy_json(out / "manifest.json", manifest)
        packets = {(row["topic_id"], row["round"]): read_json(out / row["packet_file"])
                   for row in manifest["pairs"]}
        before_packets = deepcopy(packets)
        before = tree_hashes(out)
        with self.assertRaisesRegex(ValueError, reason):
            validate_drafts(read_json(out / "inputs/additions.json"), packets)
        self.assertEqual(packets, before_packets)
        self.assertEqual(tree_hashes(out), before)

    def test_build_verifies_six_pairs_and_twelve_payloads(self):
        result = verify_dose_build(self.build)
        self.assertEqual(result, self.result["verification"])
        self.assertEqual(result, read_json(self.build / "verification.json"))
        for field in ("integrity_valid", "dose_draft_valid", "source_text_unchanged",
                      "source_map_unchanged", "all_subject_payloads_rebuilt"):
            self.assertIs(result[field], True)
        self.assertEqual((result["pairs_checked"], result["pairs_passed"]), (6, 6))
        self.assertEqual(self.manifest["status"], "dose_draft_passed")
        self.assertEqual(self.manifest["build_id"], self.build.name)
        self.assertEqual(self.build.parent, self.root / "experiment/dose_candidates")
        self.assertEqual(Path(self.manifest["evidence_build_directory"]).resolve(),
                         ORIGINAL_EVIDENCE_BUILD.resolve())
        self.assertEqual((self.manifest["topic_count"], self.manifest["distinct_evidence_count"],
                          self.manifest["pair_count"], self.manifest["payload_count"]), (3, 18, 6, 12))
        self.assertEqual(len(self.manifest["pairs"]), 6)
        self.assertEqual(len({row["pair_id"] for row in self.manifest["pairs"]}), 6)
        self.assertEqual({(row["topic_id"], row["round"]) for row in self.manifest["pairs"]},
                         set(self.packets))
        self.assertEqual(len(list((self.build / "payloads").glob("*.json"))), 12)
        self.assertIs(result["formal_eligible"], False)
        self.assertEqual(result["human_reviews_completed"], 0)
        self.assertEqual(self.manifest["model_calls_this_build"], 0)

    def test_payload_lengths_and_two_round_totals(self):
        lengths, totals = [], {}
        for row in self.manifest["pairs"]:
            for arm in ARMS:
                with self.subTest(pair=row["pair_id"], arm=arm):
                    payload = read_json(self.build / row["payload_files"][arm])
                    count = len(payload["messages"][0]["content"])
                    self.assertGreaterEqual(count, 690)
                    self.assertLessEqual(count, 796)
                    self.assertEqual(row[arm + "_visible_chars"], count)
                    lengths.append(count)
                    totals.setdefault((row["topic_id"], arm), []).append(count)
        self.assertEqual((len(lengths), min(lengths), max(lengths)), (12, 690, 796))
        self.assertEqual(len(self.manifest["topic_totals"]), 3)
        for row in self.manifest["topic_totals"]:
            for arm in ARMS:
                counts = totals[row["topic_id"], arm]
                self.assertEqual(len(counts), 2)
                self.assertGreaterEqual(sum(counts), 1200)
                self.assertLessEqual(sum(counts), 1600)
                self.assertEqual(row[arm + "_total_chars"], sum(counts))

    def test_whole_and_additional_ratios_below_ten_percent(self):
        for row in self.manifest["pairs"]:
            pair = read_json(self.build / row["pair_file"])
            whole = [len(read_json(self.build / row["payload_files"][arm])["messages"][0]["content"])
                     for arm in ARMS]
            addons = [len(pair[arm]["additional_text"]) for arm in ARMS]
            for metric, counts in (("total_difference_ratio", whole),
                                   ("additional_difference_ratio", addons)):
                with self.subTest(pair=row["pair_id"], metric=metric):
                    ratio = abs(counts[0] - counts[1]) / min(counts)
                    self.assertLess(ratio, .1)
                    self.assertAlmostEqual(row[metric], ratio)
                    self.assertAlmostEqual(pair["checks"][metric], ratio)

    def test_evidence_and_additions_preserved_verbatim(self):
        sources = {(row["source_url"], row["source_family_id"]): row
                   for row in self.source_index.values()}
        drafts = {(row["topic_id"], row["round"]): row for row in self.drafts["pairs"]}
        seen = set()
        for row in self.manifest["pairs"]:
            key = row["topic_id"], row["round"]
            packet = self.packets[key]
            self.assertEqual(read_json(self.build / row["packet_file"]), packet)
            self.assertEqual(row["packet_sha256"], canonical_hash(packet))
            blocks = []
            for entry in packet["evidence"]:
                seen.add(entry["evidence_id"])
                self.assertEqual(hashlib.sha256(entry["text"].encode("utf-8")).hexdigest(),
                                 entry["content_sha256"])
                source = sources[entry["source_url"], entry["source_family_id"]]
                label_date = source["publication_date"] or DISPLAY_POLICY["unknown_date"]
                blocks.append(f"[{entry['evidence_id']} | {source['source_id']} "
                              f"{source['publisher_short']} {label_date}]\n{entry['text']}")
            expected_block = "\n\n".join(blocks)
            self.assertEqual(compact_evidence(packet, self.source_index)["evidence_block"], expected_block)
            pair = read_json(self.build / row["pair_file"])
            self.assertEqual(pair["checks"]["integrity"]["packet"], packet)
            for arm in ARMS:
                with self.subTest(pair=row["pair_id"], arm=arm):
                    self.assertEqual(pair[arm]["evidence_block"].encode("utf-8"),
                                     expected_block.encode("utf-8"))
                    self.assertEqual(pair[arm]["additional_text"], drafts[key][arm])
                    expected = ("阅读材料\n" + expected_block + "\n\n阅读说明\n"
                                + drafts[key][arm] + "\n\n" + RESPONSE_PROMPT)
                    payload = read_json(self.build / row["payload_files"][arm])
                    self.assertEqual(payload["messages"][0]["content"].encode("utf-8"),
                                     expected.encode("utf-8"))
        self.assertEqual(len(seen), 18)

    def test_public_context_has_no_source_urls_or_arm_labels(self):
        for row in self.manifest["pairs"]:
            pair = read_json(self.build / row["pair_file"])
            self.assertEqual(pair["I4"]["evidence_block"], pair["I5"]["evidence_block"])
            for arm in ARMS:
                with self.subTest(pair=row["pair_id"], arm=arm):
                    payload = read_json(self.build / row["payload_files"][arm])
                    self.assertEqual(payload, public_payload(pair, arm))
                    self.assertEqual(set(payload), {"messages", "source_map_hash", "payload_hash",
                                                    "experimental_ready"})
                    self.assertEqual(payload["messages"], [{"role": "user", "content": pair[arm]["visible_text"]}])
                    content = payload["messages"][0]["content"]
                    self.assertNotRegex(content, r"(?i)https?://|www\.|I[45]")
                    for source in self.source_index.values():
                        self.assertNotIn(source["source_url"], content)
                        self.assertNotIn(source["source_family_id"], content)
                    self.assertEqual(content.count(RESPONSE_PROMPT), 1)
                    self.assertEqual(pair[arm]["response_opportunities"], 1)
                    self.assertIs(payload["experimental_ready"], False)
                    self.assertEqual(payload["source_map_hash"], canonical_hash(pair[arm]["source_map"]))
                    self.assertEqual(payload["payload_hash"], canonical_hash(
                        {key: value for key, value in payload.items() if key != "payload_hash"}))

    def test_source_mapping_matches_all_thirteen_original_sources(self):
        original = read_json(ORIGINAL_EVIDENCE_BUILD / "review/sources.json")["sources"]
        self.assertEqual(len(original), 13)
        self.assertEqual(len(self.source_index), 13)
        self.assertEqual(set(self.source_index), {row["source_id"] for row in original})
        for source in original:
            mapped = self.source_index[source["source_id"]]
            self.assertEqual(set(mapped), {"source_id", "publisher_short", "publication_date",
                                           "source_url", "source_family_id"})
            for new, old in (("source_id", "source_id"), ("publication_date", "publication_date"),
                             ("source_url", "url"), ("source_family_id", "source_family_id")):
                self.assertEqual(mapped[new], source[old])
            self.assertGreater(len(mapped["publisher_short"]), 0)
            self.assertLessEqual(len(mapped["publisher_short"]), 18)
        seen = set()
        for row in self.manifest["pairs"]:
            pair = read_json(self.build / row["pair_file"])
            self.assertEqual(pair["I4"]["source_map"], pair["I5"]["source_map"])
            for source in pair["I4"]["source_map"]:
                self.assertEqual(source, self.source_index[source["source_id"]])
                seen.add(source["source_id"])
        self.assertEqual(seen, set(self.source_index))

    def test_full_old_build_and_real_inputs_remain_unchanged(self):
        self.assertEqual(tree_hashes(ORIGINAL_EVIDENCE_BUILD), self.original_hashes)
        self.assertIn("manifest.json", self.original_hashes)
        self.assertIn("verification.json", self.original_hashes)
        for row in self.original_manifest["files"]:
            path = ORIGINAL_EVIDENCE_BUILD / row["path"]
            self.assertEqual(file_hash(path), row["sha256"])
            self.assertEqual(path.stat().st_size, row["bytes"])
        self.assertEqual({path: file_hash(path) for path in self.protected_hashes}, self.protected_hashes)
        self.assertEqual(self.manifest["evidence_manifest_sha256"], self.original_hashes["manifest.json"])
        self.assertEqual(tree_hashes(self.build), self.baseline_hashes)

    def test_original_quotes_are_exact_local_substrings(self):
        review = read_json(ORIGINAL_EVIDENCE_BUILD / "review/evidence_review.json")["items"]
        self.assertEqual(len(review), 18)
        packet_entries = {entry["evidence_id"]: entry
                          for packet in self.packets.values() for entry in packet["evidence"]}
        self.assertEqual({row["evidence_id"] for row in review}, set(packet_entries))
        for row in review:
            with self.subTest(evidence=row["evidence_id"]):
                source = row["source"]
                extracted = read_json(ORIGINAL_EVIDENCE_BUILD.parent / source["intake_text"])
                text, quote = extracted["text"], row["original_quote"]
                self.assertEqual(extracted["source_id"], source["source_id"])
                self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), source["text_sha256"])
                self.assertTrue(quote)
                self.assertIn(quote, text)
                self.assertEqual(text[row["quote_start"]:row["quote_end"]], quote)
                self.assertEqual(packet_entries[row["evidence_id"]]["text"], row["text"])

    def test_ai_review_bound_to_original_draft_bytes_and_all_packets(self):
        audit = read_json(self.build / "review/semantic_audit.json")
        self.assertEqual(audit, read_json(SEMANTIC_REVIEW))
        copied_review = self.root / "experiment/dose_candidates/semantic_review_v1.json"
        self.assertEqual(copied_review.read_bytes(), SEMANTIC_REVIEW.read_bytes())
        self.assertEqual(audit["status"], "pass_with_limitations")
        self.assertEqual(audit["review_type"], "independent_ai_not_human")
        self.assertEqual(audit["content_origin"], "editorial_design_draft")
        self.assertEqual(audit["input_sha256"], file_hash(DRAFTS))
        self.assertEqual(audit["input_sha256"], self.manifest["input_drafts_sha256"])
        self.assertEqual(Path(self.manifest["input_drafts_path"]).resolve(), DRAFTS.resolve())
        self.assertEqual(read_json(self.build / "inputs/additions.json"), self.drafts)
        self.assertEqual(audit["checked_pairs"], 6)
        self.assertEqual(len(audit["checks"]), 6)
        self.assertEqual({row["packet_id"] for row in audit["checks"]},
                         {packet["packet_id"] for packet in self.packets.values()})
        for row in audit["checks"]:
            self.assertIs(row["I4_supported"], True)
            self.assertIs(row["I5_supported"], True)
        self.assertEqual(audit["human_reviews_completed"], 0)
        self.assertIs(audit["approved_for_experiment"], False)
        self.assertTrue(validate_drafts(self.drafts, self.packets))

    def test_appended_duplicate_pairs_rejected(self):
        for extra_count in (1, 6):
            with self.subTest(extra_count=extra_count):
                out, manifest = self.copy_build()
                manifest["pairs"].extend(deepcopy(manifest["pairs"][0]) for _ in range(extra_count))
                manifest["pair_count"] = len(manifest["pairs"])
                manifest["payload_count"] = 2 * len(manifest["pairs"])
                self.assert_build_rejected(out, manifest, "六对轮次不完整或重复")

    def test_distinct_pair_ids_cannot_alias_first_pair(self):
        for keep_topic_rounds in (False, True):
            with self.subTest(keep_topic_rounds=keep_topic_rounds):
                out, manifest = self.copy_build()
                original_rows = deepcopy(manifest["pairs"])
                first = original_rows[0]
                manifest["pairs"] = []
                for original in original_rows:
                    row = deepcopy(first)
                    row["pair_id"] = original["pair_id"]
                    if keep_topic_rounds:
                        row.update(topic_id=original["topic_id"], round=original["round"])
                    manifest["pairs"].append(row)
                self.assertEqual(len({row["pair_id"] for row in manifest["pairs"]}), 6)
                self.assertEqual(len({row["pair_file"] for row in manifest["pairs"]}), 1)
                reason = ("pair_id、文件名与实际议题轮次不匹配" if keep_topic_rounds
                          else "实际议题轮次不完整、重复或无效")
                self.assert_build_rejected(out, manifest, reason)

    def test_inflated_summary_and_visible_counts_rejected(self):
        for field in ("topic_count", "distinct_evidence_count", "pair_count", "payload_count"):
            with self.subTest(summary=field):
                out, manifest = self.copy_build()
                manifest[field] = 999
                self.assert_build_rejected(out, manifest, "构建汇总数量不匹配")
        for field in ("I4_visible_chars", "I5_visible_chars", "old_evidence_block_chars", "new_evidence_block_chars"):
            with self.subTest(pair_count=field):
                out, manifest = self.copy_build()
                manifest["pairs"][0][field] = 999
                reason = "计字与实际消息不同" if "visible" in field else "摘要字符统计或证据列表不真实"
                self.assert_build_rejected(out, manifest, reason)

    def test_zero_topic_totals_rejected(self):
        for arm in ARMS:
            with self.subTest(arm=arm):
                out, manifest = self.copy_build()
                for row in manifest["topic_totals"]:
                    row[arm + "_total_chars"] = 0
                self.assert_build_rejected(out, manifest, "两轮累计统计与实际消息不同")

    def test_zero_additional_chars_rejected(self):
        for arm in ARMS:
            with self.subTest(arm=arm):
                out, manifest = self.copy_build()
                manifest["pairs"][0][arm + "_additional_chars"] = 0
                self.assert_build_rejected(out, manifest, "摘要字符统计或证据列表不真实")

    def test_candidate_identity_and_formal_claims_cannot_change(self):
        for field, value in (("natural_view_used", True), ("personalized_mirror_completed", True),
                             ("draft_is_not_full_I4", False), ("human_reviews_completed", -1),
                             ("formal_eligible", True), ("approved_for_experiment", True)):
            with self.subTest(field=field):
                out, manifest = self.copy_build()
                manifest[field] = value
                self.assert_build_rejected(out, manifest, "不允许改变候选稿身份")

    def test_human_signoff_cannot_be_auto_completed(self):
        for field, value in (("reviewer_1", "仅测试的伪造签署"), ("approved_for_experiment", True)):
            with self.subTest(field=field):
                out, manifest = self.copy_build()
                relative = "review/human_signoff.json"
                signoff = read_json(out / relative)
                signoff[field] = value
                self.rewrite_frozen_json(out, manifest, relative, signoff)
                self.assert_build_rejected(out, manifest, "候选构建不能自动填写人工审核")

    def test_manifest_must_cover_every_payload(self):
        for arm in ARMS:
            with self.subTest(arm=arm):
                out, manifest = self.copy_build()
                relative = manifest["pairs"][0]["payload_files"][arm]
                manifest["files"] = [row for row in manifest["files"] if row["path"] != relative]
                self.assertTrue((out / relative).is_file())
                self.assert_build_rejected(out, manifest, "清单必须覆盖且只包含")

    def test_duplicate_frozen_file_rejected(self):
        out, manifest = self.copy_build()
        manifest["files"].append(deepcopy(manifest["files"][0]))
        self.assert_build_rejected(out, manifest, "重复清单文件")

    def test_frozen_file_traversal_rejected(self):
        out, manifest = self.copy_build()
        record = manifest["files"][0]
        escaped = self.mutation_root / "escaped.json"
        shutil.copy2(out / record["path"], escaped)
        record["path"] = "../escaped.json"
        self.assertEqual(file_hash(escaped), record["sha256"])
        self.assertEqual(escaped.stat().st_size, record["bytes"])
        self.assert_build_rejected(out, manifest, "构建引用越界")

    def test_source_date_change_rejected_with_packets_unchanged(self):
        out, manifest = self.copy_build()
        packet_hashes = {row["packet_file"]: file_hash(out / row["packet_file"])
                         for row in manifest["pairs"]}
        relative = "inputs/source_index.json"
        source_index = read_json(out / relative)
        source = next(row for row in source_index.values() if row["publication_date"] is not None)
        source["publication_date"] = (date.fromisoformat(source["publication_date"]) + timedelta(days=1)).isoformat()
        self.rewrite_frozen_json(out, manifest, relative, source_index)
        self.assertEqual({path: file_hash(out / path) for path in packet_hashes}, packet_hashes)
        self.assert_build_rejected(out, manifest, "短来源表与原始链接、文献日期或家族不一致")

    def test_semantic_review_status_and_input_hash_rechecked_after_rehash(self):
        for mutation in ("status", "missing_input_sha256", "wrong_input_sha256"):
            with self.subTest(mutation=mutation):
                out, manifest = self.copy_build()
                relative = "review/semantic_audit.json"
                audit = read_json(out / relative)
                if mutation == "status":
                    audit["status"] = "fail"
                elif mutation == "missing_input_sha256":
                    del audit["input_sha256"]
                else:
                    audit["input_sha256"] = "0" * 64
                self.rewrite_frozen_json(out, manifest, relative, audit)
                self.assert_build_rejected(out, manifest, "独立复核结果未绑定完整候选稿")

    def test_wrong_addon_packet_ids_rejected_by_validate_drafts(self):
        for index in range(6):
            with self.subTest(pair=index):
                out, manifest = self.copy_build()
                drafts = read_json(out / "inputs/additions.json")
                drafts["pairs"][index]["packet_id"] = drafts["pairs"][(index + 1) % 6]["packet_id"]
                self.assert_drafts_rejected(out, manifest, drafts, "草稿绑定的证据包不一致")

    def test_unknown_and_cross_round_citations_rejected_by_validate_drafts(self):
        for arm in ARMS:
            for mutation in ("unknown", "cross_round", "undeclared"):
                with self.subTest(arm=arm, mutation=mutation):
                    out, manifest = self.copy_build()
                    drafts = read_json(out / "inputs/additions.json")
                    first = drafts["pairs"][0]
                    citation = first[arm + "_citations"][0]
                    replacement = (drafts["pairs"][1][arm + "_citations"][0]
                                   if mutation == "cross_round" else "CL_E999")
                    self.assertNotIn(replacement, {row["evidence_id"] for row in
                                     self.packets[first["topic_id"], first["round"]]["evidence"]})
                    first[arm] = first[arm].replace(citation, replacement)
                    if mutation != "undeclared":
                        first[arm + "_citations"][0] = replacement
                    reason = "正文引用与声明不一致" if mutation == "undeclared" else "附加段声明引用有误"
                    self.assert_drafts_rejected(out, manifest, drafts, reason)

    def test_i5_question_rejected_by_validate_drafts(self):
        for question_mark in ("?", "？"):
            with self.subTest(question_mark=question_mark):
                out, manifest = self.copy_build()
                drafts = read_json(out / "inputs/additions.json")
                drafts["pairs"][0]["I5"] += "还需要补充什么" + question_mark
                self.assert_drafts_rejected(out, manifest, drafts, "普通复述不添加独有问题或回应机会")


if __name__ == "__main__":
    unittest.main()
