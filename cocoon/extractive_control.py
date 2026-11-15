"""独立的离线探索性对照，不修复或覆盖失败的 v3 四句模型对照。

共同证据与回应机会不变；组织侧原四句不改，对照每条证据摘录一整句。
只返回数据，由主控另行决定是否保存。哈希仅核对本地一致性，不是外部
网络真实性审计，也不代表语义、人审或正式实验资格通过。
"""

from copy import deepcopy
import hashlib
from itertools import product
from math import prod
from pathlib import Path
import re

from . import delivery_audit
from .dosage import RESPONSE_PROMPT, compact_evidence
from .runtime import _parse_json, _reject_sensitive_keys, _verify_blob, canonical_hash


_POLICY = {
    "schema_version": "1.0",
    "version": "extractive_control_v1",
    "design_status": "exploratory_alternative_after_v3_failure",
    "source_organized_output": "recorded_and_validated_only",
    "control_content_origin": "deterministic_exact_source_sentences",
    "source_scope": "same_frozen_packet_only",
    "display_version": "source_id_inline_v1",
    "sentence_rule": "按。！？划分完整句，保留正文全部字符及顺序；每条证据恰好选一整句，不改字、不新增连接语。",
    "selection_rule": "先枚举每条证据的完整句，按证据顺序及句子偏移做字典序笛卡尔积；只按最终600–800、总长和附加长10%约束，选第一个合格组合。保存所有候选的偏移及字数，不按结论方向或语义评分挑选。",
    "candidate_combination_limit": 10000,
    "total_min_chars": 600,
    "total_max_chars": 800,
    "match_tolerance": 0.1,
    "text_rewriting": False,
    "model_calls": 0,
    "old_runs_unchanged": True,
    "natural_view_used": False,
    "full_I4_eligible": False,
    "human_reviews_completed": 0,
    "formal_eligible": False,
    "protocol_note": "这是独立的探索性I5实现备选。普通阅读段采用原证据句而不是四句模型生成，因此不沿用v3分句数量/每句长度契约；原方案最终阅读量、共同证据、来源和回应机会要求保持。绑定组织侧原输出不修改；不把v3失败重标成功。该对照版本仍须人工确认与正式登记。",
}
_CHECK_NAMES = (
    "evidence_equal", "source_equal", "dose_within_range", "length_matched",
    "additional_length_matched", "response_equal", "assembly_valid",
)


def _require(condition, code):
    if not condition:
        raise ValueError("extractive_control:" + code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _validate_policy(policy):
    _require(type(policy) is dict and set(policy) == set(_POLICY), "policy_fields_mismatch")
    _require(all(type(policy[k]) is type(v) and policy[k] == v for k, v in _POLICY.items()),
             "policy_value_mismatch")


def _load_policy(path, expected_sha):
    _require(type(expected_sha) is str and re.fullmatch(r"[0-9a-f]{64}", expected_sha),
             "policy_hash_mismatch")
    data = delivery_audit._file(path).read_bytes()
    _require(_sha(data) == expected_sha, "policy_hash_mismatch")
    policy = _parse_json(data)
    _validate_policy(policy)
    return policy


def _sentence_spans(text):
    """偏移按 Python 字符计；尾部纯空白归入末句，任何非空白残片都拒绝。"""
    _require(type(text) is str and bool(text), "invalid_sentence_text")
    spans, cursor = [], 0
    for match in re.finditer(r'.*?[。！？](?:[”’"])?', text, re.DOTALL):
        _require(match.start() == cursor, "incomplete_source_sentence")
        spans.append((match.start(), match.end()))
        cursor = match.end()
    _require(bool(spans) and (cursor == len(text) or text[cursor:].isspace()),
             "incomplete_source_sentence")
    spans[-1] = (spans[-1][0], len(text))
    _require("".join(text[start:end] for start, end in spans) == text, "sentence_coverage_mismatch")
    return spans


def select_exact_sentences(evidence, organized_additional_chars, fixed_chars, policy):
    """只接收证据和整数长度预算，不接收任一模型正文，不作语义择优。"""
    _validate_policy(policy)
    _require(type(evidence) is list and bool(evidence), "empty_evidence")
    _require(type(organized_additional_chars) is int and organized_additional_chars > 0
             and type(fixed_chars) is int and fixed_chars >= 0, "invalid_length_budget")
    pools = []
    for entry in evidence:
        candidates = []
        for index, (start, end) in enumerate(_sentence_spans(entry["text"])):
            text = entry["text"][start:end]
            # 普通阅读不选问句；保留原句序号，不把过滤后的序号冒充原始偏移。
            if "？" in text or "?" in text:
                continue
            candidates.append({
                "sentence_index": index, "evidence_id": entry["evidence_id"],
                "text": text, "start": start, "end": end,
                "source_url": entry["source_url"], "source_family_id": entry["source_family_id"],
            })
        pools.append(candidates)
    combination_count = prod(len(pool) for pool in pools)
    _require(combination_count <= policy["candidate_combination_limit"], "combination_limit_exceeded")
    selected, selected_indices, log = [], None, []
    organized_visible_chars = fixed_chars + organized_additional_chars
    low, high, tolerance = policy["total_min_chars"], policy["total_max_chars"], policy["match_tolerance"]
    for number, combination in enumerate(product(*pools)):
        # 未选组合仅计字诊断；引用渲染开销也必须进入预算。
        size = sum(len(s["text"]) + len(s["evidence_id"]) + 2 for s in combination)
        visible_size = fixed_chars + size
        additional_ratio = abs(organized_additional_chars - size) / min(organized_additional_chars, size)
        total_ratio = abs(organized_visible_chars - visible_size) / min(organized_visible_chars, visible_size)
        valid = (additional_ratio <= tolerance and low <= organized_visible_chars <= high
                 and low <= visible_size <= high and total_ratio <= tolerance)
        indices = [s["sentence_index"] for s in combination]
        log.append({
            "combination_index": number, "sentence_indices": indices,
            "offsets": [{k: s[k] for k in ("evidence_id", "start", "end")} for s in combination],
            "additional_chars": size, "visible_chars": visible_size,
            "total_difference_ratio": total_ratio, "additional_difference_ratio": additional_ratio,
            "valid": valid,
        })
        if valid and selected_indices is None:
            selected = [{k: v for k, v in s.items() if k != "sentence_index"} for s in combination]
            selected_indices = indices[:]
    return {
        "status": "structurally_matched_candidate" if selected_indices is not None else "no_matching_candidate",
        "selected_sentences": selected, "selected_sentence_indices": selected_indices,
        "candidate_combination_count": combination_count, "candidate_log": log,
    }


def _visible(block, addition):
    return "阅读材料\n" + block + "\n\n阅读说明\n" + addition + "\n\n" + RESPONSE_PROMPT


def _source_files(audited, policy_path, expected_sha):
    inputs = audited["source_inputs"]
    files = {delivery_audit._file(policy_path): expected_sha}
    for key in ("policy_input", "packet_input", "source_index_input"):
        info = inputs[key]
        files[delivery_audit._file(info["source_path"])] = info["file_sha256"]
    info = inputs["packet_input"]
    manifest_path = delivery_audit._file(info["manifest_source_path"])
    manifest_bytes = manifest_path.read_bytes()
    _require(_sha(manifest_bytes) == info["manifest_sha256"], "source_manifest_changed")
    files[manifest_path] = info["manifest_sha256"]
    for entry in _parse_json(manifest_bytes)["files"]:
        path = delivery_audit._file(manifest_path.parent / entry["path"])
        _require(path.is_relative_to(manifest_path.parent), "unsafe_source_path")
        files[path] = entry["sha256"]
    return files


def build_extractive_alternative(run_directory, policy_path, expected_policy_sha) -> dict:
    """完整复核旧记录后构造新备选；失败对照只核验轨迹，绝不用于选句。"""
    policy = _load_policy(policy_path, expected_policy_sha)
    root = Path(run_directory).resolve(strict=True)
    run_before = delivery_audit._hashes(root)
    audited = delivery_audit.inspect_delivery_run(root)
    _require(audited["audit_status"] == "passed" and audited["inputs_match"] is True
             and audited["internal_report_preserved"] is True
             and audited["original_snapshot"]["file_hashes_match"] is True, "source_audit_not_preserved")
    _require(audited["policy_version"] == "report_to_delivery_v3"
             and audited["run_status"] == "delivery_failed" and audited["pair_available"] is False,
             "source_not_failed_v3")
    organized = audited["by_mode"]["organized_reflection"]
    plain = audited["by_mode"]["plain_restatement"]
    _require(organized["accepted"] is True and organized["status"] == "validated"
             and plain["accepted"] is False and plain["status"] == "failed", "source_modes_not_eligible")
    summary_bytes = delivery_audit._file(root / "summary.json").read_bytes()
    _require(_sha(summary_bytes) == audited["summary_sha256"] == run_before["summary.json"],
             "source_summary_changed")
    summary = _parse_json(summary_bytes)
    refs = summary["inputs"]
    _require(summary["inputs_unchanged"] is True and canonical_hash(refs)
             == canonical_hash(audited["source_inputs"]["stored_refs"]), "source_inputs_mismatch")
    # 运行时读取冻结 blob，不从当前源文件或模型诊断正文构造摘录。
    packet = _verify_blob(root, refs["packet"])
    index = _verify_blob(root, refs["source_index"])
    configuration = audited["source_inputs"]["configuration"]
    packet_sha, index_sha = canonical_hash(packet), canonical_hash(index)
    _require(packet_sha == refs["packet"]["sha256"] == configuration["packet_sha256"]
             == audited["source_inputs"]["packet_input"]["canonical_sha256"]
             and index_sha == refs["source_index"]["sha256"] == configuration["source_index_sha256"],
             "source_hash_mismatch")
    compact = compact_evidence(packet, index)
    block = compact["evidence_block"]
    _require(block == audited["evidence_block"] and compact["source_map"] == audited["source_map"]
             and compact["packet_sha256"] == packet_sha, "source_map_mismatch")
    fixed = len(_visible(block, ""))
    _require(fixed == audited["fixed_chars"], "fixed_assembly_mismatch")
    row = summary["additions"]["organized_reflection"]
    raw = _verify_blob(root, row["response"])
    parsed = _parse_json(raw["raw_text"])
    _require(canonical_hash(parsed) == canonical_hash(organized["parsed_output"]), "organized_raw_mismatch")
    organized_text = "".join(s["text"] + "（" + "、".join(s["citations"]) + "）" for s in parsed["sentences"])
    _require(organized_text == row["rendered"]["additional_text"]
             and len(organized_text) == row["rendered"]["char_count"], "organized_render_mismatch")
    source_ids = [row["source_event_id"]]
    _require(source_ids == summary["source_event_ids"], "organized_events_mismatch")

    parent = Path(audited["source_inputs"]["parent_source_path"]).resolve(strict=True)
    parent_before = delivery_audit._hashes(parent)
    _require(parent_before == _verify_blob(root, refs["parent_file_hashes"]), "parent_files_changed")
    files = _source_files(audited, policy_path, expected_policy_sha)
    source_before = {str(p): _sha(p.read_bytes()) for p in files}
    _require(source_before == {str(p): digest for p, digest in files.items()}, "source_files_changed")
    before = {"run": run_before, "parent_run": parent_before, "inputs": source_before}

    selection = select_exact_sentences(packet["evidence"], len(organized_text), fixed, policy)
    origins = {"organized": "recorded_assistant_output", "plain": "deterministic_exact_source_sentences"}
    checks = {name: False for name in _CHECK_NAMES}
    checks.update(total_difference_ratio=None, additional_difference_ratio=None)
    pair = None
    if selection["status"] == "structurally_matched_candidate":
        sentences = selection["selected_sentences"]
        plain_text = "".join(s["text"] + "（" + s["evidence_id"] + "）" for s in sentences)
        pair = {}
        for arm, mode, text in (("I4", "organized", organized_text), ("I5", "plain", plain_text)):
            visible = _visible(block, text)
            _require(not re.search(r"I[45]", visible, re.IGNORECASE), "treatment_label_in_visible_text")
            pair[arm] = {"additional_text": text, "visible_text": visible, "char_count": len(visible),
                         "additional_chars": len(text), "content_origin": origins[mode]}
        left, right = pair["I4"], pair["I5"]
        total_ratio = abs(left["char_count"] - right["char_count"]) / min(left["char_count"], right["char_count"])
        additional_ratio = abs(left["additional_chars"] - right["additional_chars"]) / min(
            left["additional_chars"], right["additional_chars"])
        prefix, suffix = "阅读材料\n" + block + "\n\n阅读说明\n", "\n\n" + RESPONSE_PROMPT
        sources = {(s["source_url"], s["source_family_id"]) for s in compact["source_map"]}
        checks.update(
            evidence_equal=all(p["visible_text"][:len(prefix)] == prefix for p in pair.values())
                           and [s["evidence_id"] for s in sentences] == compact["evidence_ids"],
            source_equal=all((s["source_url"], s["source_family_id"]) in sources for s in sentences),
            dose_within_range=all(policy["total_min_chars"] <= p["char_count"] <= policy["total_max_chars"]
                                  for p in pair.values()),
            length_matched=total_ratio <= policy["match_tolerance"],
            additional_length_matched=additional_ratio <= policy["match_tolerance"],
            response_equal=all(p["visible_text"][-len(suffix):] == suffix for p in pair.values()),
            total_difference_ratio=total_ratio, additional_difference_ratio=additional_ratio,
        )
        checks["assembly_valid"] = all(checks[k] for k in _CHECK_NAMES if k != "assembly_valid") and all(
            p["visible_text"] == prefix + p["additional_text"] + suffix
            and p["char_count"] == len(p["visible_text"]) == fixed + p["additional_chars"]
            and p["additional_chars"] == len(p["additional_text"]) for p in pair.values())
        _require(checks["assembly_valid"], "independent_assembly_failed")

    after = {"run": delivery_audit._hashes(root), "parent_run": delivery_audit._hashes(parent),
             "inputs": {str(p): _sha(p.read_bytes()) for p in files}}
    _require(before == after, "source_snapshot_changed")
    result = {
        "schema_version": policy["schema_version"], "alternative_version": policy["version"], **selection,
        "source_run_id": audited["run_id"], "source_summary_sha256": audited["summary_sha256"],
        "policy_sha256": expected_policy_sha, "packet_sha256": packet_sha, "source_index_sha256": index_sha,
        "content_origins": origins, "organized_source_event_ids": source_ids,
        "pair": pair, "source_map": compact["source_map"], "evidence_block": block, "checks": checks,
        "plain_generated_by_model": False, "source_original_run_status": "delivery_failed",
        "source_plain_attempt_status": "failed", "scope": "new_offline_exploratory_variant_not_repaired_v3",
        "new_model_calls": 0, "human_reviews_completed": 0, "full_I4_eligible": False,
        "formal_eligible": False, "subject_delivered": False,
        "protocol_note": policy["protocol_note"], "semantic_review": "pending_independent_review",
        "sentence_counts": {"organized": len(parsed["sentences"]), "plain": len(selection["selected_sentences"])},
        "source_audit": {k: audited[k] for k in ("audit_status", "inputs_match", "internal_report_preserved", "original_snapshot")},
        "source_snapshots": {"before_sha256": canonical_hash(before), "after_sha256": canonical_hash(after),
                             "unchanged": True},
    }
    _reject_sensitive_keys(result)
    return deepcopy(result)


def verify_extractive_alternative(data, run_directory, policy_path, expected_policy_sha) -> bool:
    """从原始轨迹完全重建并严格比较；仅忽略主控附加的顶层 created_at_utc。"""
    _require(type(data) is dict, "invalid_result")
    snapshot = deepcopy(data)
    _reject_sensitive_keys(snapshot)
    snapshot.pop("created_at_utc", None)
    expected = build_extractive_alternative(run_directory, policy_path, expected_policy_sha)
    _require(canonical_hash(snapshot) == canonical_hash(expected), "result_mismatch")
    return True


_FEASIBILITY_CASES = (
    ("TOPIC_01", 1), ("TOPIC_01", 2),
    ("TOPIC_02", 1), ("TOPIC_02", 2),
    ("TOPIC_03", 1), ("TOPIC_03", 2),
)
_LENGTH_COVERAGE_NOTE = "仅为允许组织侧附加段整数长度的覆盖比例，不是模型成功率或语义通过率。"


def _feasibility_ranges(lengths):
    """压缩整数并集为闭区间；相邻整数可合并，缺失整数不可跨越。"""
    ranges = []
    for value in sorted(set(lengths)):
        if ranges and value == ranges[-1][1] + 1:
            ranges[-1][1] = value
        else:
            ranges.append([value, value])
    return ranges


def _feasibility_length_match(fixed, organized, plain):
    return (organized > 0 and plain > 0
            and 600 <= fixed + organized <= 800 and 600 <= fixed + plain <= 800
            and 10 * abs(organized - plain) <= min(organized, plain)
            and 10 * abs((fixed + organized) - (fixed + plain))
            <= min(fixed + organized, fixed + plain))


def _inspect_extractive_lengths(evidence, fixed, policy):
    """理论长度枚举；既不接收组织侧正文，也不构造任何实际阅读配对。"""
    _validate_policy(policy)
    _require(type(evidence) is list and bool(evidence), "empty_evidence")
    _require(type(fixed) is int and fixed >= 0, "invalid_length_budget")
    sentence_pools, eligible_pools = [], []
    for entry in evidence:
        sentences = []
        for number, (start, end) in enumerate(_sentence_spans(entry["text"])):
            text = entry["text"][start:end]
            sentences.append({
                "sentence_index": number, "evidence_id": entry["evidence_id"],
                "text": text, "start": start, "end": end,
                "source_url": entry["source_url"], "source_family_id": entry["source_family_id"],
                "selector_eligible": "？" not in text and "?" not in text,
            })
        sentence_pools.append({
            "evidence_id": entry["evidence_id"], "content_origin": "冻结中文证据正文整句",
            "source_url": entry["source_url"], "source_family_id": entry["source_family_id"],
            "text_sha256": _sha(entry["text"].encode("utf-8")), "sentences": sentences,
        })
        eligible_pools.append([s for s in sentences if s["selector_eligible"]])
    total = prod(len(pool) for pool in eligible_pools)
    _require(total <= policy["candidate_combination_limit"], "combination_limit_exceeded")
    combinations = []
    for number, combination in enumerate(product(*eligible_pools)):
        size = sum(len(s["text"]) + len(s["evidence_id"]) + 2 for s in combination)
        combinations.append({
            "combination_id": f"combo_{number:05d}", "combination_index": number,
            "sentence_indices": [s["sentence_index"] for s in combination],
            "offsets": [{k: s[k] for k in ("evidence_id", "start", "end")} for s in combination],
            "plain_additional_chars": size, "plain_visible_chars": fixed + size,
            "dose_in_range": 600 <= fixed + size <= 800,
            "matching_organized_ranges": [],
        })
    low, high = max(1, 600 - fixed), 800 - fixed
    matching_lengths = [[] for _ in combinations]
    first_selection, feasible = {}, []
    for organized in range(low, high + 1):
        selection = select_exact_sentences(evidence, organized, fixed, policy)
        _require(selection["candidate_combination_count"] == total
                 and len(selection["candidate_log"]) == total, "selector_enumeration_mismatch")
        first = None
        for combo, log in zip(combinations, selection["candidate_log"]):
            valid = _feasibility_length_match(fixed, organized, combo["plain_additional_chars"])
            _require(log["combination_index"] == combo["combination_index"]
                     and log["sentence_indices"] == combo["sentence_indices"]
                     and log["offsets"] == combo["offsets"]
                     and log["additional_chars"] == combo["plain_additional_chars"]
                     and log["visible_chars"] == combo["plain_visible_chars"]
                     and log["valid"] is valid, "selector_integer_mismatch")
            if valid:
                matching_lengths[combo["combination_index"]].append(organized)
                if first is None:
                    first = combo["combination_index"]
        expected_indices = combinations[first]["sentence_indices"] if first is not None else None
        expected_sentences = [] if first is None else [
            {k: v for k, v in pool["sentences"][index].items()
             if k not in ("sentence_index", "selector_eligible")}
            for pool, index in zip(sentence_pools, expected_indices)
        ]
        _require(selection["selected_sentence_indices"] == expected_indices
                 and selection["selected_sentences"] == expected_sentences
                 and selection["status"] == ("structurally_matched_candidate" if first is not None
                                             else "no_matching_candidate"), "selector_first_mismatch")
        first_selection[str(organized)] = first
        if first is not None:
            feasible.append(organized)
    for combo, lengths in zip(combinations, matching_lengths):
        combo["matching_organized_ranges"] = _feasibility_ranges(lengths)
    allowed_count = max(0, high - low + 1)
    return {
        "fixed_chars": fixed, "allowed_org_range": [low, high] if allowed_count else None,
        "sentence_pools": sentence_pools, "candidate_combinations": combinations,
        "total_combinations": total,
        "dose_eligible_combinations": sum(c["dose_in_range"] for c in combinations),
        "feasible_organized_ranges": _feasibility_ranges(feasible),
        "feasible_length_count": len(feasible), "total_allowed_length_count": allowed_count,
        "coverage_ratio": len(feasible) / allowed_count if allowed_count else 0.0,
        "coverage_note": _LENGTH_COVERAGE_NOTE,
        "first_selection_by_organized_length": first_selection,
        "conditional_length_feasible": bool(feasible),
        "actual_organized_output_available": False, "actual_pair_available": False,
        "actual_organized_chars": None, "actual_plain_chars": None,
        "actual_organized_total_chars": None, "actual_plain_total_chars": None,
        "actual_combination_index": None, "actual_source_run_id": None,
        "actual_source_event_ids": None, "actual_content_origin": None,
        "status": "conditional_only" if feasible else "no_length_solution",
        "full_I4_eligible": False, "formal_eligible": False, "no_semantic_approval": True,
    }


def _feasibility_read(path, files, expected_sha=None, expected_size=None):
    """登记实际读取字节；重复读取必须仍与首次快照一致。"""
    path = delivery_audit._file(path)
    data = path.read_bytes()
    digest = _sha(data)
    _require(expected_sha is None or digest == expected_sha, "feasibility_input_hash_mismatch")
    _require(expected_size is None or len(data) == expected_size, "feasibility_input_size_mismatch")
    key = str(path)
    _require(key not in files or files[key] == digest, "source_snapshot_changed")
    files[key] = digest
    return data


def inspect_extractive_feasibility(evidence_build, policy_path, expected_policy_sha, *,
                                   recorded_alternative_path=None, recorded_run_directory=None) -> dict:
    """六轮同策略的整数长度可行域；可选旧记录不代表本轮模型生成成功。"""
    from .dose_build import source_index_from_review
    from .evidence import verify_bundle

    _require((recorded_alternative_path is None) == (recorded_run_directory is None),
             "recorded_arguments_incomplete")
    policy = _load_policy(policy_path, expected_policy_sha)
    policy_file = delivery_audit._file(policy_path)
    files = {str(policy_file): expected_policy_sha}
    root = Path(evidence_build).resolve(strict=True)
    manifest_file = delivery_audit._file(root / "manifest.json")
    manifest = _parse_json(_feasibility_read(manifest_file, files))
    frozen = {}
    for record in manifest["files"]:
        relative = record["path"]
        _require(type(relative) is str and not Path(relative).is_absolute()
                 and relative not in frozen, "invalid_manifest_path")
        path = delivery_audit._file(root / relative)
        _require(path.is_relative_to(root) and path != manifest_file, "unsafe_source_path")
        frozen[relative] = _feasibility_read(path, files, record["sha256"], record["bytes"])
    topics = manifest["topics"]
    _require(len(topics) == 3 and {t["topic_id"] for t in topics}
             == {"TOPIC_01", "TOPIC_02", "TOPIC_03"}, "feasibility_topics_mismatch")
    topics = {t["topic_id"]: t for t in topics}
    for topic_id in topics:
        for field, suffix in (("full_packet", "full"), ("round_1_packet", "round_1"),
                              ("round_2_packet", "round_2")):
            relative = f"packets/{topic_id}_{suffix}.json"
            _require(topics[topic_id][field] == relative and relative in frozen, "feasibility_packet_set_mismatch")
    _require("review/sources.json" in frozen, "feasibility_sources_missing")
    verified = verify_bundle(root)
    _require(verified["passed"] is True and verified["packet_count"] == 9
             and verified["topic_count"] == 3, "feasibility_bundle_invalid")
    index = source_index_from_review(root)
    sources = _parse_json(frozen["review/sources.json"])["sources"]
    _require(set(index) == {s["source_id"] for s in sources}
             and all(index[s["source_id"]]["source_url"] == s["url"]
                     and index[s["source_id"]]["source_family_id"] == s["source_family_id"]
                     and index[s["source_id"]]["publication_date"] == s["publication_date"] for s in sources),
             "feasibility_source_index_mismatch")
    index_sha = canonical_hash(index)
    recorded, recorded_before = None, None
    run = parent = None
    if recorded_alternative_path is not None:
        recorded = _parse_json(_feasibility_read(recorded_alternative_path, files))
        run = Path(recorded_run_directory).resolve(strict=True)
        run_before = delivery_audit._hashes(run)
        summary = _parse_json(_feasibility_read(run / "summary.json", files, run_before["summary.json"]))
        parent = Path(summary["parent_source_path"]).resolve(strict=True)
        parent_before = delivery_audit._hashes(parent)
        source_files = _source_files({"source_inputs": summary}, policy_path, expected_policy_sha)
        original_inputs = {}
        for path, digest in source_files.items():
            _feasibility_read(path, files, digest)
            original_inputs[str(path)] = digest
        recorded_before = {"run": run_before, "parent_run": parent_before, "inputs": original_inputs}
        for directory, hashes in ((run, run_before), (parent, parent_before)):
            for relative, digest in hashes.items():
                _feasibility_read(directory / relative, files, digest)
        _require(verify_extractive_alternative(recorded, run, policy_path, expected_policy_sha) is True,
                 "recorded_alternative_invalid")
        _require(canonical_hash(recorded_before) == recorded["source_snapshots"]["before_sha256"],
                 "recorded_snapshot_mismatch")
    before = dict(files)
    cases = []
    for topic_id, round_no in _FEASIBILITY_CASES:
        relative = f"packets/{topic_id}_round_{round_no}.json"
        packet = _parse_json(frozen[relative])
        _require(packet["topic_id"] == topic_id, "feasibility_packet_identity_mismatch")
        compact = compact_evidence(packet, index)
        case_inputs = {
            "files": {str(path): files[str(path)] for path in (
                manifest_file, delivery_audit._file(root / relative), policy_file,
                delivery_audit._file(root / "review/sources.json"))},
            "packet_sha256": canonical_hash(packet), "source_index_sha256": index_sha,
            "display_policy_sha256": compact["policy_sha256"],
        }
        case = {
            "case_id": f"{topic_id}_R{round_no}", "topic_id": topic_id, "round": round_no,
            "packet_id": packet["packet_id"], "packet_sha256": canonical_hash(packet),
            "title": packet["topic"],
            "title_metadata": {"packet_topic": packet["topic"],
                               "manifest_candidate_title": topics[topic_id]["candidate_title"],
                               "time_scope": topics[topic_id]["time_scope"]},
            "source_map": compact["source_map"], "input_hashes": case_inputs,
            **_inspect_extractive_lengths(packet["evidence"], len(_visible(compact["evidence_block"], "")), policy),
        }
        cases.append(case)
    if recorded is not None:
        first = cases[0]
        _require(recorded["packet_sha256"] == first["packet_sha256"]
                 and sum(c["packet_sha256"] == recorded["packet_sha256"] for c in cases) == 1
                 and recorded["source_index_sha256"] == index_sha
                 and recorded["source_map"] == first["source_map"], "recorded_packet_mismatch")
        _require(recorded["status"] == "structurally_matched_candidate" and recorded["pair"] is not None
                 and recorded["full_I4_eligible"] is False, "recorded_pair_unavailable")
        organized, plain = recorded["pair"]["I4"], recorded["pair"]["I5"]
        chosen = first["first_selection_by_organized_length"].get(str(organized["additional_chars"]))
        _require(chosen is not None, "recorded_length_not_feasible")
        combo = first["candidate_combinations"][chosen]
        _require(combo["sentence_indices"] == recorded["selected_sentence_indices"]
                 and combo["plain_additional_chars"] == plain["additional_chars"]
                 and combo["plain_visible_chars"] == plain["char_count"]
                 and first["fixed_chars"] + organized["additional_chars"] == organized["char_count"]
                 and _feasibility_length_match(first["fixed_chars"], organized["additional_chars"],
                                               plain["additional_chars"]), "recorded_selection_mismatch")
        first.update(
            actual_organized_output_available=True, actual_pair_available=True,
            actual_organized_chars=organized["additional_chars"], actual_plain_chars=plain["additional_chars"],
            actual_organized_total_chars=organized["char_count"], actual_plain_total_chars=plain["char_count"],
            actual_combination_index=chosen, actual_source_run_id=recorded["source_run_id"],
            actual_source_event_ids=recorded["organized_source_event_ids"],
            actual_content_origin="existing_recorded_exploratory_alternative_not_generated_this_inspection",
            status="recorded_exploratory_candidate",
        )
        alt_file = delivery_audit._file(recorded_alternative_path)
        first["input_hashes"]["files"][str(alt_file)] = files[str(alt_file)]
        first["input_hashes"].update(recorded_source_snapshot_sha256=canonical_hash(recorded_before),
                                     recorded_summary_sha256=recorded["source_summary_sha256"])
    for case in cases:
        case["input_sha256"] = canonical_hash(case["input_hashes"])
    after = {path: _sha(delivery_audit._file(path).read_bytes()) for path in before}
    _require(before == after, "source_snapshot_changed")
    if recorded_before is not None:
        _require(delivery_audit._hashes(run) == recorded_before["run"]
                 and delivery_audit._hashes(parent) == recorded_before["parent_run"], "source_snapshot_changed")
    feasible_count = sum(c["feasible_length_count"] for c in cases)
    allowed_count = sum(c["total_allowed_length_count"] for c in cases)
    summary = {
        "case_count": len(cases),
        "conditional_length_feasible_cases": sum(c["conditional_length_feasible"] for c in cases),
        "no_length_solution_cases": sum(c["status"] == "no_length_solution" for c in cases),
        "conditional_only_cases": sum(c["status"] == "conditional_only" for c in cases),
        "recorded_exploratory_candidate_cases": sum(c["status"] == "recorded_exploratory_candidate" for c in cases),
        "actual_organized_output_available_cases": sum(c["actual_organized_output_available"] for c in cases),
        "actual_pair_available_cases": sum(c["actual_pair_available"] for c in cases),
        "feasible_length_count": feasible_count, "total_allowed_length_count": allowed_count,
        "coverage_ratio": feasible_count / allowed_count if allowed_count else 0.0,
        "coverage_note": _LENGTH_COVERAGE_NOTE,
    }
    result = {
        "schema_version": "1.0", "version": "extractive_feasibility_v1",
        "policy_version": policy["version"], "scope": "offline_integer_length_feasibility_not_model_success",
        "evidence_build_id": manifest["build_id"],
        "input_hashes": {"files": before, "policy_sha256": expected_policy_sha,
                         "manifest_sha256": before[str(manifest_file)],
                         "sources_sha256": before[str(delivery_audit._file(root / "review/sources.json"))],
                         "source_index_sha256": index_sha,
                         "recorded_alternative_sha256": None if recorded is None else before[str(alt_file)]},
        "source_verification": verified, "cases": cases, "summary": summary,
        "new_model_calls": 0, "cost": 0, "human_reviews_completed": 0,
        "full_I4_eligible": False, "formal_eligible": False, "no_semantic_approval": True,
        "source_unchanged": True,
        "source_snapshots": {"before_sha256": canonical_hash(before), "after_sha256": canonical_hash(after),
                             "unchanged": True},
        "protocol_limits": {
            "coverage": _LENGTH_COVERAGE_NOTE,
            "sentence_origin": "冻结中文证据正文整句；含中文释义或改编，不标作英文来源原句。",
            "selection": "完整枚举每条证据的整句，排除问句；按冻结策略取首个合格字典序组合，最多10000组合。",
            "length_basis": "Python len，含引用、标签、标点、空白及同一回应提示；总长600..800，附加长和总长均用10*abs(a-b)<=min(a,b)。",
            "actual": "无真实记录就没有实际组织侧或配对；旧备选仅绑定首包，不是本轮生成的实际模型成功。",
            "scope": "不评估性价比，不扩展主题，不添加政治、财务或其他外部新事实。",
            "approval": "仅核对本地冻结输入和长度；不证明网络真实性、语义、人工审批或完整I4，正式资格仍为false。",
        },
    }
    _reject_sensitive_keys(result)
    return deepcopy(result)


def verify_extractive_feasibility(data, evidence_build, policy_path, expected_policy_sha, *,
                                  recorded_alternative_path=None, recorded_run_directory=None) -> bool:
    """不信任结果内的摘要、实际状态或输入hash，重新读源全量重建后比较canonical。"""
    _require(type(data) is dict, "invalid_feasibility_result")
    snapshot = deepcopy(data)
    _reject_sensitive_keys(snapshot)
    expected = inspect_extractive_feasibility(
        evidence_build, policy_path, expected_policy_sha,
        recorded_alternative_path=recorded_alternative_path, recorded_run_directory=recorded_run_directory,
    )
    _require(canonical_hash(snapshot) == canonical_hash(expected), "feasibility_result_mismatch")
    return True
