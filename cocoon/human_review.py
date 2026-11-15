"""只读生成人审准备模板；不签署、不选句、不写文件，不代表完整108卡审查。"""

from collections import defaultdict
from copy import deepcopy
import hashlib
from pathlib import Path
import re

from .contracts import parse_stage_output
from .evidence import _quote_count, verify_bundle
from .runtime import _parse_json, _reject_sensitive_keys, canonical_hash

ROOT = Path(__file__).resolve().parents[1]
_STAGES = ("search", "label", "blindspot", "integrate")
_VERDICTS = ("supported", "acceptable_gap_or_question", "unsupported_fact", "overstated", "needs_review")
_GROUPS = ("evidence_reviews", "source_reviews", "model_issue_reviews", "round_reviews", "release_gates")
_EVIDENCE_CHECKS = ("原文支持", "中文释义", "数字日期否定模态", "引用对应", "语境保留")
_SOURCE_CHECKS = ("一二手标注", "版本时点", "独立性及同源转引", "可访问证据", "版权与再分发用途")
_MODEL_CHECKS = ("原证据是否支持", "是否措辞过强", "是否需要修改", "是否可接受")
_CONTEXT_CHECKS = (
    "每条摘录均为对应证据的完整原句", "代词指代与因果承接仍完整", "删去条件或限定的风险",
    "同屏原证据仍完整", "两侧相同来源与回应机会", "没有真实T1不得伪造个性化镜像",
    "最终全文600至800字符", "全文与附加段长度差异均不超过10%",
)


def _require(condition, code):
    if not condition:
        raise ValueError("human_review:" + code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _equal(left, right):
    return canonical_hash(left) == canonical_hash(right)


def _local_path(root, value):
    text = str(value).replace("\\", "/")
    _require(bool(text) and not text.startswith("//"), "unsafe_path")
    candidate = Path(text)
    _require(".." not in candidate.parts, "unsafe_path")
    candidate = candidate if candidate.is_absolute() else root / candidate
    _require(candidate.is_relative_to(root), "path_outside_root")
    resolved = candidate.resolve(strict=True)
    _require(resolved.is_relative_to(root), "path_outside_root")
    return resolved


def _safe_id(value):
    _require(type(value) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value), "unsafe_item_id")
    return value


def _card(identifier, descriptor, checks, **fields):
    reviewer = {"reviewer_id": None, "reviewed_at": None, "decision": "pending",
                "notes": None, "checks": dict.fromkeys(checks)}
    return {"id": identifier, "source_input_descriptor": deepcopy(descriptor), **fields,
            "reviewer_1": deepcopy(reviewer), "reviewer_2": deepcopy(reviewer),
            "adjudication": {"status": "not_started", "adjudicator_id": None,
                             "decision": "pending", "reason": None},
            "human_reviews_completed": 0}


def build_human_review_checklist(evidence_build: Path, live_semantic_path: Path) -> dict:
    """只返回可序列化的待填写模板。普通哈希只证明本地一致性，不是数字签名。

    仅读取指定证据包、原来源快照、语义意见及其run_id限定的summary和八个
    阶段引用blob；不重复执行整目录运行审计。最终JSON及只读页面由主控保存。
    """
    build = _local_path(ROOT, evidence_build)
    semantic_path = _local_path(ROOT, live_semantic_path)
    verified = verify_bundle(build)
    _require(verified["passed"] is True, "bundle_verification_failed")
    snapshots = {}

    def read(path):
        path = _local_path(ROOT, path)
        data = path.read_bytes()
        name = path.as_posix()
        digest = _sha(data)
        _require(name not in snapshots or snapshots[name] == digest, "input_changed")
        snapshots[name] = digest
        return data

    def load(path):
        value = _parse_json(read(path))
        canonical_hash(value)
        _reject_sensitive_keys(value)
        return value

    def descriptor(path, pointer="", value=None):
        result = {"path": path.as_posix(), "sha256": snapshots[path.as_posix()], "json_pointer": pointer}
        if value is not None:
            result["object_sha256"] = canonical_hash(value)
        return result

    manifest_path = build / "manifest.json"
    manifest = load(manifest_path)
    indexed_files = {}
    for entry in manifest["files"]:
        path = _local_path(build, entry["path"])
        _require(path not in indexed_files, "duplicate_manifest_file")
        data = read(path)
        _require(_sha(data) == entry["sha256"] and len(data) == entry["bytes"], "bundle_hash_mismatch")
        indexed_files[path] = entry
    evidence_path, sources_path = build / "review/evidence_review.json", build / "review/sources.json"
    _require(evidence_path in indexed_files and sources_path in indexed_files, "unbound_review_input")
    evidence_document, sources_document = load(evidence_path), load(sources_path)
    evidence, sources = evidence_document["items"], sources_document["sources"]
    _require(type(evidence) is list and type(sources) is list, "invalid_review_lists")
    _require(len(evidence) == manifest["evidence_count"] == verified["evidence_count"]
             and len(sources) == manifest["selected_source_count"], "bundle_counts_mismatch")
    _require(evidence_document["contains_final_test_answers"] is False
             and evidence_document["approved_for_experiment"] is False
             and _equal(evidence_document["human_reviews_completed"], 0), "not_pending_evidence")
    by_evidence = {row["evidence_id"]: row for row in evidence}
    by_source = {row["source_id"]: row for row in sources}
    _require(len(by_evidence) == len(evidence) and len(by_source) == len(sources), "duplicate_id")
    source_root = _local_path(ROOT, manifest["source_root"])
    intake = _local_path(source_root, "intake")
    family_members, source_evidence = defaultdict(list), defaultdict(list)
    for row in evidence:
        _safe_id(row["evidence_id"])
        _require(row["source_id"] in by_source, "missing_source")
        source_evidence[row["source_id"]].append(row["evidence_id"])
    for row in sources:
        _safe_id(row["source_id"])
        family_members[row["source_family_id"]].append(row["source_id"])

    source_cards, source_texts, source_blobs = [], {}, {}
    for index, source in enumerate(sources):
        sid = source["source_id"]
        paths = {name: _local_path(intake, source_root / Path(source["intake_" + name].replace("\\", "/")))
                 for name in ("meta", "raw", "text")}
        metadata, text_document = load(paths["meta"]), load(paths["text"])
        raw = read(paths["raw"])
        text = text_document["text"]
        _require(metadata["source_id"] == text_document["source_id"] == sid, "source_identity_mismatch")
        _require(metadata["outcome"] == "downloaded_text_extracted" and metadata["http_status"] == 200
                 and metadata["process_returncode"] == 0, "source_capture_incomplete")
        _require(_sha(raw) == metadata["raw_sha256"] == source["raw_sha256"]
                 and len(raw) == metadata["bytes"], "source_raw_hash_mismatch")
        _require(_sha(text.encode("utf-8")) == metadata["text_sha256"] == source["text_sha256"]
                 and len(text) == metadata["text_char_count"], "source_text_hash_mismatch")
        _require(metadata["requested_url"] == source["url"]
                 and metadata["effective_url"] == source["effective_url"]
                 and metadata["completed_at_utc"] == source["retrieved_at_utc"], "source_metadata_mismatch")
        _require(all(_local_path(intake, metadata[name + "_path"]) == paths[name]
                     for name in ("raw", "text")), "source_blob_path_mismatch")
        source_texts[sid] = text
        source_blobs[sid] = {name: descriptor(path) for name, path in paths.items()}
        source_cards.append(_card(
            "SRC_" + sid, descriptor(sources_path, f"/sources/{index}", source), _SOURCE_CHECKS,
            **{key: deepcopy(source[key]) for key in (
                "source_id", "title", "url", "publication_date", "source_level", "source_family_id", "license_note")},
            metadata=deepcopy(source), capture_metadata=metadata, input_paths=source_blobs[sid],
            evidence_ids=source_evidence[sid], same_family_source_ids=family_members[source["source_family_id"]],
            independence_status="pending_human_review_not_inferred_from_distinct_ids",
            rights_status="pending_human_review_no_legal_conclusion"))

    evidence_cards = []
    for index, row in enumerate(evidence):
        sid, eid = row["source_id"], row["evidence_id"]
        source = by_source[sid]
        _require(_equal(row["source"], source), "embedded_source_mismatch")
        _require(row["human_review_1"] is None and row["human_review_2"] is None
                 and row["adjudication"] is None and _equal(row["human_reviews_completed"], 0), "evidence_already_reviewed")
        start, end, quote = row["quote_start"], row["quote_end"], row["original_quote"]
        _require(type(start) is int and type(end) is int and 0 <= start < end <= len(source_texts[sid])
                 and source_texts[sid][start:end] == quote, "quote_offset_mismatch")
        _require(_sha(row["text"].encode("utf-8")) == row["content_sha256"], "evidence_text_hash_mismatch")
        evidence_cards.append(_card(
            "EV_" + eid, descriptor(evidence_path, f"/items/{index}", row), _EVIDENCE_CHECKS,
            **{key: deepcopy(row[key]) for key in (
                "evidence_id", "topic_id", "source_id", "text", "original_quote", "boundary_note", "quote_start",
                "quote_end", "review_dimension", "transformation_type", "content_sha256")},
            **{key: deepcopy(source[key]) for key in ("title", "url", "publication_date", "source_family_id")},
            source_review_id="SRC_" + sid, blob_paths=deepcopy(source_blobs[sid]),
            source_text_sha256=source["text_sha256"], quote_offset_unit="python_unicode_code_points",
            quote_budget={key: source[key] for key in ("language", "direct_quote_units", "direct_quote_limit")}))
    for sid, source in by_source.items():
        quote_units = _quote_count(" ".join(by_evidence[eid]["original_quote"] for eid in source_evidence[sid]), source["language"])
        _require(quote_units == source["direct_quote_units"] <= source["direct_quote_limit"], "source_quote_budget_mismatch")

    round_cards, packets, original_ids = [], {}, set()
    for topic_index, topic in enumerate(manifest["topics"]):
        topic_id = _safe_id(topic["topic_id"])
        material_path = _local_path(intake, source_root / Path(topic["source_material"].replace("\\", "/")))
        material = load(material_path)
        _require(snapshots[material_path.as_posix()] == topic["source_material_sha256"], "original_material_hash_mismatch")
        for original in material["evidence"]:
            eid = original["evidence_id"]
            _require(eid in by_evidence and eid not in original_ids, "original_evidence_id_mismatch")
            _require(_equal({key: by_evidence[eid][key] for key in original}, original)
                     and by_evidence[eid]["topic_id"] == topic_id, "original_evidence_changed")
            original_ids.add(eid)
        for number in (1, 2):
            packet_path = _local_path(build, topic[f"round_{number}_packet"])
            _require(packet_path in indexed_files, "unbound_packet")
            packet = load(packet_path)
            _require(len(packet["evidence"]) == 3, "round_evidence_count_mismatch")
            for entry in packet["evidence"]:
                original = by_evidence[entry["evidence_id"]]
                source = by_source[original["source_id"]]
                expected = {key: original[key] for key in ("evidence_id", "text", "content_sha256")}
                expected.update(source_url=source["url"], source_family_id=source["source_family_id"])
                _require(_equal(entry, expected) and original["topic_id"] == topic_id, "packet_evidence_mismatch")
            packet_hash = canonical_hash(packet)
            _require(packet_hash not in packets, "duplicate_round_packet")
            packet_descriptor = descriptor(packet_path, "", packet)
            packets[packet_hash] = (packet, packet_descriptor)
            evidence_ids = [entry["evidence_id"] for entry in packet["evidence"]]
            round_cards.append(_card(
                f"ROUND_{topic_id}_{number}", packet_descriptor, _CONTEXT_CHECKS,
                topic_id=topic_id, round=number, packet_id=packet["packet_id"],
                packet_sha256=packet_hash, packet_file_sha256=packet_descriptor["sha256"],
                evidence_ids=evidence_ids, evidence=deepcopy(packet["evidence"]),
                evidence_review_ids=["EV_" + eid for eid in evidence_ids], stance_axis=deepcopy(packet["stance_axis"]),
                topic_input_descriptor=descriptor(manifest_path, f"/topics/{topic_index}", topic),
                original_material_descriptor=descriptor(material_path, "", material),
                context_checks=dict.fromkeys(_CONTEXT_CHECKS), selected_sentences=None,
                main_page_cross_mapping={"status": "pending_main_controller",
                                         "organized_output_descriptor": None, "extractive_output_descriptor": None},
                dose={"min_chars": 600, "max_chars": 800, "total_match_tolerance": 0.1,
                      "additional_match_tolerance": 0.1, "status": "pending"},
                natural_T1_available=None, full_I4_eligible=False,
                note="仅列摘录语境核对要求；不选句、不代选组织侧输出，具体真实输出由主控交叉映射。"))
    _require(original_ids == set(by_evidence) and len(round_cards) == 6, "round_coverage_mismatch")

    semantic = load(semantic_path)
    _require(semantic["review_type"] == "independent_ai_not_human"
             and _equal(semantic["human_reviews_completed"], 0) and semantic["formal_eligible"] is False
             and semantic["no_model_calls"] is True, "semantic_identity_mismatch")
    run_id = semantic["run_id"]
    _require(type(run_id) is str and re.fullmatch(r"assistant_probe_[0-9]{8}T[0-9]{6}_[0-9a-f]{8}", run_id), "unsafe_run_id")
    run_parent = _local_path(ROOT, "experiment/mimo_probes")
    run = _local_path(run_parent, run_id)
    summary_path = _local_path(run, "summary.json")
    summary = load(summary_path)
    _require(snapshots[summary_path.as_posix()] == semantic["summary_sha256"], "summary_hash_mismatch")
    _require(summary["run_id"] == run_id and _local_path(run_parent, summary["run_directory"]) == run
             and summary["kind"] == "assistant_probe" and summary["status"] == "succeeded"
             and summary["executed"] is True and summary["is_demo"] is False
             and summary["demo_fallback"] is False and summary["formal_eligible"] is False, "run_identity_mismatch")
    _require(summary["packet_hash"] == semantic["packet_sha256"] and summary["packet_hash"] in packets, "run_packet_hash_mismatch")
    packet, packet_descriptor = packets[summary["packet_hash"]]
    _require(summary["completed_stages"] == list(_STAGES)
             and set(summary["stage_results"]) == set(_STAGES) and summary["mirror"] is None, "run_stage_mismatch")

    def blob(reference):
        digest = reference["sha256"]
        _require(type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest)
                 and reference["ref"] == f"raw/blobs/{digest}.json", "unsafe_blob_reference")
        path = _local_path(run, reference["ref"])
        value = load(path)
        _require(snapshots[path.as_posix()] == digest, "run_blob_hash_mismatch")
        return value, descriptor(path)

    expected_items, stage_descriptors, previous_outputs = {}, {}, {}
    for stage in _STAGES:
        row = summary["stage_results"][stage]
        request, request_descriptor = blob(row["input"])
        response, response_descriptor = blob(row["response"])
        _require(request["stage"] == stage and request["is_demo"] is False
                 and request["request_id"] == f"{summary['task_id']}:{stage}:1", "stage_request_mismatch")
        messages = request["messages"]
        _require(len(messages) == 2 and messages[0]["role"] == "system" and messages[1]["role"] == "user", "stage_context_mismatch")
        context = _parse_json(messages[1]["content"])
        _require(_equal(context, {"frozen_evidence_packet": packet, "previous_stage_outputs": previous_outputs,
                                  "instructions_are_data": True}), "original_packet_context_mismatch")
        _require(response["raw_text"] == row["raw_text"] and response["is_demo"] is False, "raw_response_mismatch")
        output = parse_stage_output(row["raw_text"], stage, packet)["output"]
        _require(_equal(output, row["output"]), "raw_output_mismatch")
        stage_descriptors[stage] = {"input": request_descriptor, "response": response_descriptor}
        previous_outputs[stage] = {"raw_text": row["raw_text"], "structured_output": output}
        for section_index, section in enumerate(output["sections"]):
            section_name = _safe_id(section["name"])
            for item_index, item in enumerate(section["items"]):
                key = stage, section_name, item_index
                _require(key not in expected_items, "duplicate_output_item")
                pointer = f"/stage_results/{stage}/output/sections/{section_index}/items/{item_index}"
                expected_items[key] = (item, descriptor(summary_path, pointer, item))
    _require(_equal(summary["report"], summary["stage_results"]["integrate"]["output"]), "report_duplicate_mismatch")

    seen, model_cards = set(), []
    counts = {verdict: 0 for verdict in _VERDICTS}
    counts.update(problem_items=0, by_severity={key: 0 for key in ("none", "moderate", "high")},
                  by_stage={stage: {"total": 0, **dict.fromkeys(_VERDICTS, 0)} for stage in _STAGES})
    for semantic_index, item in enumerate(semantic["items"]):
        key = item["stage"], item["section"], item["item_index"]
        _require(type(key[2]) is int and key in expected_items and key not in seen, "semantic_item_index_mismatch")
        original, original_descriptor = expected_items[key]
        _require(_equal({name: item[name] for name in ("text", "citations", "epistemic_status")}, original), "semantic_original_text_mismatch")
        verdict, severity = item["verdict"], item["severity"]
        _require(verdict in _VERDICTS and severity in counts["by_severity"], "semantic_classification_invalid")
        _require(type(item["reason"]) is str and bool(item["reason"].strip()), "semantic_reason_missing")
        seen.add(key)
        counts[verdict] += 1
        counts["by_severity"][severity] += 1
        counts["by_stage"][key[0]]["total"] += 1
        counts["by_stage"][key[0]][verdict] += 1
        if verdict in ("unsupported_fact", "overstated", "needs_review"):
            counts["problem_items"] += 1
            model_cards.append(_card(
                f"MODEL_{key[0]}_{key[1]}_{key[2]}", descriptor(semantic_path, f"/items/{semantic_index}", item),
                _MODEL_CHECKS, **{name: deepcopy(item[name]) for name in (
                    "stage", "section", "item_index", "text", "citations", "epistemic_status", "verdict", "reason", "severity")},
                original_output_descriptor=original_descriptor,
                stage_input_descriptor=deepcopy(stage_descriptors[key[0]]["input"]),
                stage_response_descriptor=deepcopy(stage_descriptors[key[0]]["response"]),
                packet_descriptor=deepcopy(packet_descriptor),
                evidence_review_ids=["EV_" + eid for eid in item["citations"]],
                assessment_role="ai_opinion_only_human_decision_pending"))
    counts.update(stages_reviewed=len(_STAGES), raw_text_output_equal_stages=len(_STAGES),
                  report_duplicate_items_not_recounted=sum(len(section["items"]) for section in summary["report"]["sections"]))
    _require(seen == set(expected_items) and _equal(semantic["items_reviewed"], len(seen)), "semantic_coverage_mismatch")
    _require(_equal(semantic["counts"], counts), "semantic_counts_mismatch")
    _require(semantic["status"] == ("protocol_deviations_found" if counts["problem_items"] else "pass_with_limitations"), "semantic_status_mismatch")

    gate_definitions = (
        ("DISTINCT_REVIEWERS", "双人独立且不得同人双签", "两名真实审核者身份与时间可核验，姓名或身份不得重复；AI不能充任人工审核者。"),
        ("THIRD_ADJUDICATOR", "分歧由独立第三人裁决", "保留两份独立意见；分歧交由不同于前两人的第三人，记录理由，不自动多数表决。"),
        ("SOURCE_RIGHTS", "来源许可与用途人审", "逐来源核对一二手、时点、可访问证据与再分发用途；公开可访问不等于许可，机器不作法律结论。"),
        ("TOPIC_AXIS", "跨议题立场轴适用性", "分别核查三议题轴和U或极化指标的适用性，不从机制事实推出价值立场，不把同源材料当独立印证。"),
        ("GOLD_ISOLATION", "独立gold及最终测验隔离", "独立金标准、锚题及最终答案另建并隔离；当前仅审核准备，不是每议题完整108卡审查。"),
        ("REAL_T1_FULL_I4", "真实T1与完整I4送达", "使用真实自然T1观点与完整I4流程；不得伪造镜像，主控另行绑定真实输出并复核等证据、来源、回应及最终剂量。"),
        ("OASIS_MODEL_BUDGET", "OASIS模型与预算冻结", "另行核实OASIS实际模型、配置、预算及冻结版本；当前离线模板不选择模型、不调用模型、不完成冻结。"),
    )
    manifest_descriptor = descriptor(manifest_path, "", manifest)
    gates = [_card("GATE_" + code, manifest_descriptor, (title,), title=title, required_action=action,
                   scope="outside_current_lightweight_template", automatic_completion=False)
             for code, title, action in gate_definitions]
    groups = dict(zip(_GROUPS, (evidence_cards, source_cards, model_cards, round_cards, gates)))
    breakdown = {name: len(items) for name, items in groups.items()}
    total = sum(breakdown.values())
    all_ids = [item["id"] for items in groups.values() for item in items]
    _require(len(set(all_ids)) == total, "duplicate_checklist_id")
    before = dict(sorted(snapshots.items()))
    after = {name: _sha(_local_path(ROOT, name).read_bytes()) for name in before}
    _require(before == after, "inputs_changed_during_build")
    result = {
        "schema_version": "human_review_template_v1", "status": "pending_human_review",
        "use_scope": "reviewer_only_never_subject", "contains_final_test_answers": False,
        "human_reviews_completed": 0, "approved_for_experiment": False, "formal_eligible": False,
        "new_model_calls": 0, "no_model_output_edits": True, "full_108_card_review": False,
        "counts": {**breakdown, "total_items": total, "double_human_slots": total * 2,
                   "completed_items": 0, "completed_human_slots": 0},
        "live_semantic_items_reviewed": len(seen), "live_semantic_counts": counts,
        "source_family_groups": dict(sorted(family_members.items())),
        "source_family_count": len(family_members), "source_family_count_is_independence_proof": False,
        "input_descriptors": {"evidence_manifest": manifest_descriptor,
                              "evidence_review": descriptor(evidence_path, "", evidence_document),
                              "sources": descriptor(sources_path, "", sources_document),
                              "live_semantic": descriptor(semantic_path, "", semantic),
                              "run_summary": descriptor(summary_path, "", summary),
                              "run_packet": packet_descriptor},
        "source_inputs": before,
        "input_integrity": {"before": deepcopy(before), "after": after, "unchanged": True,
                            "is_digital_signature": False,
                            "scope": "仅读取文件的本地前后哈希一致性；非来源真实性、语义或人审通过证明，也不提供文件锁。"},
        "review_policy": {"independent_humans_required": 2, "same_person_double_signing_allowed": False,
                          "disagreement_requires_third_human": True, "ai_can_be_human_reviewer": False,
                          "machine_checks_can_approve": False,
                          "model_decisions": {"supported": "支持", "overstated": "过强",
                                              "needs_revision": "需改", "acceptable": "可接受"},
                          "validation_scope": "initial_unfilled_template_only",
                          "future_completed_reviews": "另存新版本并另行实现完成判定，不使用本模板校验器判定已填写审核。"},
        **groups,
    }
    _reject_sensitive_keys(result)
    return deepcopy(result)


def validate_human_review_template(data, evidence_build: Path, live_semantic_path: Path) -> bool:
    """仅判断未填写初始模板是否严格等于重建结果；不接受任何人工签署或完成态。"""
    try:
        return type(data) is dict and _equal(data, build_human_review_checklist(evidence_build, live_semantic_path))
    except (ValueError, OSError, KeyError, TypeError, IndexError, OverflowError, RecursionError):
        return False


def validate_human_review_checklist(data, evidence_build: Path, live_semantic_path: Path) -> bool:
    """兼容主控接口名称；语义仅限validate_human_review_template，不是完成态验证。"""
    return validate_human_review_template(data, evidence_build, live_semantic_path)


# 以下为已填写副本的独立API；上方空白模板构建与校验行为保持不变。
from datetime import datetime
from functools import wraps
import unicodedata


HUMAN_REVIEW_WORKFLOW_VERSION = "human_review_submission_v1"
_REVIEW_SLOTS = ("reviewer_1", "reviewer_2")
_REVIEW_DECISIONS = {"approved", "needs_revision", "rejected"}
_MODEL_REVIEW_DECISIONS = {"supported", "overstated", "needs_revision", "acceptable"}
_PASS_DECISIONS = {"approved", "supported", "acceptable"}
_MACHINE_IDENTIFIERS = ("ai", "llm", "gpt", "gemini", "mimo", "claude", "assistant",
                        "bot", "auto", "machine", "人工智能", "机器人", "自动", "模型")
_REVIEW_STATES = {
    "pending_human_review", "in_progress", "awaiting_adjudication",
    "resolved_with_findings", "content_review_passed_pending_release",
    "lightweight_review_passed_not_formally_eligible",
}
_DERIVED_REVIEW_KEYS = {
    "workflow_version", "template_sha256", "item_results", "identity_sets",
    "identity_verified", "content_review_passed", "release_gates_passed",
    "all_55_items_resolved", "all_55_items_passed", "release_blockers",
    "counter_definitions", "verification_scope", "source_unchanged",
}
_MUTABLE_REVIEW_TOP = {"counts", "status", "human_reviews_completed",
                       "approved_for_experiment", "formal_eligible"}
_REVIEW_COUNTERS = {
    "completed_human_slots": 110, "completed_items": 55, "dual_reviewed_items": 55,
    "disagreement_items": 55, "awaiting_adjudication_items": 55, "adjudicated_items": 55,
    "passed_items": 55, "needs_revision_items": 55, "rejected_items": 55,
    "pending_items": 55,
}


def _review_api(function):
    """公共API错误不回显提交者的姓名、意见或原始异常。"""
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ValueError as exc:
            if str(exc).startswith("human_review:"):
                raise
            raise ValueError("human_review:invalid_submission") from None
        except (OSError, KeyError, TypeError, IndexError, OverflowError, RecursionError):
            raise ValueError("human_review:invalid_submission") from None
    return call


def _review_load(value, files):
    """只接受项目内本地路径或JSON字典；绝不取URL、不写输入。"""
    if type(value) is dict:
        document = deepcopy(value)
    else:
        _require(isinstance(value, (str, Path)), "submission_type_invalid")
        path = _local_path(ROOT, value)
        _require(path.is_file() and path.stat().st_size <= 8 * 1024 * 1024, "submission_file_invalid")
        raw = path.read_bytes()
        _require(len(raw) <= 8 * 1024 * 1024, "submission_file_too_large")
        digest = _sha(raw)
        _require(path not in files or files[path] == digest, "submission_changed")
        files[path] = digest
        document = _parse_json(raw)
    _require(type(document) is dict, "submission_type_invalid")
    _reject_sensitive_keys(document)
    canonical_hash(document)
    return document


def _review_authority(evidence_build, live_semantic_path):
    authority = build_human_review_checklist(evidence_build, live_semantic_path)
    _require([len(authority[g]) for g in _GROUPS] == [18, 13, 11, 6, 7], "authority_scope_mismatch")
    _require(set(authority["review_policy"]["model_decisions"]) == _MODEL_REVIEW_DECISIONS,
             "authority_decisions_mismatch")
    _require(authority["full_108_card_review"] is False and authority["formal_eligible"] is False,
             "unsupported_formal_review_scope")
    return authority


def _review_finish(authority, files):
    # 在返回前复核本次提交路径及重建权威模板读过的全部源文件；不声称加锁。
    for name, digest in authority["source_inputs"].items():
        path = _local_path(ROOT, name)
        _require(path not in files or files[path] == digest, "source_binding_conflict")
        _require(_sha(path.read_bytes()) == digest, "source_changed_during_verification")
    for path, digest in files.items():
        _require(_sha(path.read_bytes()) == digest, "submission_changed")


def _review_shape(document, authority):
    """所有不可填内容逐字段、逐类型与权威模板对比，衍生摘要只重算。"""
    _require(set(authority) <= set(document) <= set(authority) | _DERIVED_REVIEW_KEYS,
             "submission_fields_mismatch")
    immutable = set(authority) - set(_GROUPS) - _MUTABLE_REVIEW_TOP
    _require(_equal({k: document[k] for k in immutable}, {k: authority[k] for k in immutable}),
             "immutable_template_changed")
    if "workflow_version" in document:
        _require(document["workflow_version"] == HUMAN_REVIEW_WORKFLOW_VERSION, "workflow_version_mismatch")
    if "template_sha256" in document:
        _require(document["template_sha256"] == canonical_hash(authority), "template_hash_mismatch")
    _require(type(document["status"]) is str and document["status"] in _REVIEW_STATES, "summary_status_invalid")
    for flag in ("approved_for_experiment", "formal_eligible"):
        _require(type(document[flag]) is bool, "summary_flag_invalid")
    _require(type(document["human_reviews_completed"]) is int
             and 0 <= document["human_reviews_completed"] <= 2, "summary_count_invalid")
    counters, original = document["counts"], authority["counts"]
    _require(type(counters) is dict and set(original) <= set(counters) <= set(original) | set(_REVIEW_COUNTERS),
             "counter_fields_mismatch")
    for key, value in counters.items():
        _require(type(value) is int, "counter_type_invalid")
        if key in _REVIEW_COUNTERS:
            _require(0 <= value <= _REVIEW_COUNTERS[key], "counter_range_invalid")
        else:
            _require(value == original[key], "immutable_count_changed")
    for group in _GROUPS:
        items, baseline = document[group], authority[group]
        _require(type(items) is list and len(items) == len(baseline), "item_set_changed")
        for item, blank in zip(items, baseline):
            _require(type(item) is dict and set(item) == set(blank), "item_fields_changed")
            fixed = set(blank) - set(_REVIEW_SLOTS) - {"adjudication", "human_reviews_completed"}
            _require(_equal({k: item[k] for k in fixed}, {k: blank[k] for k in fixed}),
                     "immutable_item_changed")
            _require(type(item["human_reviews_completed"]) is int
                     and 0 <= item["human_reviews_completed"] <= 2, "item_count_invalid")
            for slot in _REVIEW_SLOTS:
                filled, empty = item[slot], blank[slot]
                _require(type(filled) is dict and set(filled) == set(empty), "slot_fields_changed")
                _require(type(filled["checks"]) is dict and set(filled["checks"]) == set(empty["checks"]),
                         "check_keys_changed")
            adj = item["adjudication"]
            _require(type(adj) is dict and set(blank["adjudication"]) <= set(adj)
                     <= set(blank["adjudication"]) | {"checks"}, "adjudication_fields_changed")
            if "checks" in adj:
                _require(type(adj["checks"]) is dict
                         and set(adj["checks"]) == set(blank["reviewer_1"]["checks"]),
                         "adjudication_check_keys_changed")


def _review_text(value, code, maximum=8000):
    _require(type(value) is str and bool(value.strip()) and len(value) <= maximum, code)
    _require(not any(unicodedata.category(c) in ("Cs", "Cf") or (ord(c) < 32 and c not in "\n\r\t")
                     for c in value), code)


def _review_identity(value):
    _review_text(value, "reviewer_id_invalid", 128)
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    _require(bool(normalized) and any(c.isalnum() for c in normalized)
             and all(unicodedata.category(c)[0] in "LMN" or c in " _.-" for c in normalized),
             "reviewer_id_invalid")
    compact = "".join(c for c in normalized if c not in " _.-")
    _require(not any(token in compact for token in _MACHINE_IDENTIFIERS), "machine_reviewer_id")
    return normalized


def _review_timestamp(value):
    _require(type(value) is str and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
        value) is not None, "reviewed_at_invalid")
    if not value.endswith("Z"):
        _require(int(value[-5:-3]) <= 23 and int(value[-2:]) <= 59, "reviewed_at_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "reviewed_at_invalid")
    except ValueError:
        raise ValueError("human_review:reviewed_at_invalid") from None


def _review_slot(slot, group):
    decision = slot["decision"]
    allowed = _MODEL_REVIEW_DECISIONS if group == "model_issue_reviews" else _REVIEW_DECISIONS
    _require(type(decision) is str and decision in allowed | {"pending"}, "decision_invalid")
    pending = decision == "pending"
    _require(all(type(v) is bool or (pending and v is None) for v in slot["checks"].values()),
             "check_values_invalid")
    identifier = None
    if slot["reviewer_id"] is not None:
        identifier = _review_identity(slot["reviewer_id"])
    if slot["reviewed_at"] is not None:
        _review_timestamp(slot["reviewed_at"])
        _require(identifier is not None, "timestamp_without_reviewer")
    if slot["notes"] is not None:
        _review_text(slot["notes"], "notes_invalid")
    if not pending:
        _require(identifier is not None and slot["reviewed_at"] is not None, "completed_slot_missing_identity")
        # 所有完成态都要求意见，需改/拒绝/过强也不能省略。
        _review_text(slot["notes"], "completed_slot_notes_required")
        if decision in _PASS_DECISIONS:
            _require(all(v is True for v in slot["checks"].values()), "passing_decision_has_false_check")
    return not pending, identifier


def _review_adjudication(adj, group, dispute, left, right):
    state = adj["status"]
    _require(type(state) is str and state in {"not_started", "pending", "completed"},
             "adjudication_status_invalid")
    if state != "completed":
        _require(adj["adjudicator_id"] is None and adj["decision"] == "pending"
                 and adj["reason"] is None and "checks" not in adj, "pending_adjudication_has_content")
        return None, None
    _require(dispute, "adjudication_without_disagreement")
    identifier = _review_identity(adj["adjudicator_id"])
    allowed = _MODEL_REVIEW_DECISIONS if group == "model_issue_reviews" else _REVIEW_DECISIONS
    _require(type(adj["decision"]) is str and adj["decision"] in allowed, "adjudication_decision_invalid")
    _review_text(adj["reason"], "adjudication_reason_required")
    if "checks" in adj:
        checks = adj["checks"]
        _require(all(type(v) is bool for v in checks.values()), "adjudication_check_values_invalid")
    else:
        # 仅决定不同而检查一致，可沿用一致检查；检查本身有分歧时须逐项裁决。
        _require(_equal(left["checks"], right["checks"]), "adjudication_checks_required")
        checks = left["checks"]
    if adj["decision"] in _PASS_DECISIONS:
        _require(all(v is True for v in checks.values()), "passing_adjudication_has_false_check")
    return identifier, deepcopy(checks)


def _review_evaluate(document, authority):
    _review_shape(document, authority)
    result = deepcopy(authority)
    identities = {key: set() for key in (*_REVIEW_SLOTS, "adjudicator")}
    counts = {key: 0 for key in _REVIEW_COUNTERS}
    item_results = {}
    pass_by_group = dict.fromkeys(_GROUPS, 0)
    completed_columns = dict.fromkeys(_REVIEW_SLOTS, 0)
    for group in _GROUPS:
        for item, output in zip(document[group], result[group]):
            for key in (*_REVIEW_SLOTS, "adjudication"):
                output[key] = deepcopy(item[key])
            completed = []
            for slot in _REVIEW_SLOTS:
                done, identifier = _review_slot(item[slot], group)
                completed.append(done)
                completed_columns[slot] += int(done)
                if identifier is not None:
                    identities[slot].add(identifier)
            count = sum(completed)
            counts["completed_human_slots"] += count
            output["human_reviews_completed"] = count
            both = count == 2
            left, right = item["reviewer_1"], item["reviewer_2"]
            dispute = both and (left["decision"] != right["decision"] or not _equal(left["checks"], right["checks"]))
            counts["dual_reviewed_items"] += int(both)
            counts["disagreement_items"] += int(dispute)
            adjudicator, resolved_checks = _review_adjudication(item["adjudication"], group, dispute, left, right)
            final_decision, basis = None, None
            if adjudicator is not None:
                identities["adjudicator"].add(adjudicator)
                final_decision = item["adjudication"]["decision"]
                basis = "independent_third_reviewer"
                counts["adjudicated_items"] += 1
            elif dispute:
                state = "awaiting_adjudication"
                counts["awaiting_adjudication_items"] += 1
            elif both:
                final_decision = left["decision"]
                resolved_checks, basis = deepcopy(left["checks"]), "two_reviewers_agree"
            else:
                state = "pending" if count == 0 else "partially_reviewed"
                counts["pending_items"] += 1
            if final_decision is not None:
                counts["completed_items"] += 1
                if final_decision in _PASS_DECISIONS:
                    state = "passed"
                    counts["passed_items"] += 1
                    pass_by_group[group] += 1
                elif final_decision == "rejected":
                    state = "rejected"
                    counts["rejected_items"] += 1
                else:
                    state = "needs_revision"
                    counts["needs_revision_items"] += 1
            item_results[item["id"]] = {
                "group": group, "completed_human_slots": count, "disagreement_detected": dispute,
                "review_state": state, "final_decision": final_decision,
                "final_checks": resolved_checks, "resolution_basis": basis,
            }
    _require(not identities["reviewer_1"] & identities["reviewer_2"], "reviewer_roles_not_independent")
    _require(not identities["adjudicator"] & (identities["reviewer_1"] | identities["reviewer_2"]),
             "adjudicator_not_independent")
    _require(counts["completed_items"] == counts["passed_items"] + counts["needs_revision_items"] + counts["rejected_items"]
             and counts["completed_items"] + counts["awaiting_adjudication_items"] + counts["pending_items"] == 55,
             "derived_count_invariant_failed")
    content_passed = all(pass_by_group[g] == len(authority[g]) for g in _GROUPS[:-1])
    gates_passed = pass_by_group["release_gates"] == 7
    all_resolved, all_passed = counts["completed_items"] == 55, counts["passed_items"] == 55
    if all_passed:
        status = "lightweight_review_passed_not_formally_eligible"
    elif counts["awaiting_adjudication_items"]:
        status = "awaiting_adjudication"
    elif content_passed:
        status = "content_review_passed_pending_release"
    elif all_resolved:
        status = "resolved_with_findings"
    elif counts["completed_human_slots"]:
        status = "in_progress"
    else:
        status = "pending_human_review"
    blockers = ["identity_authenticity_not_verified", "full_108_card_review_incomplete"]
    if not content_passed:
        blockers.append("content_review_not_passed")
    if not gates_passed:
        blockers.append("release_gates_not_passed")
    result.update(
        workflow_version=HUMAN_REVIEW_WORKFLOW_VERSION, template_sha256=canonical_hash(authority),
        status=status, counts={**authority["counts"], **counts}, item_results=item_results,
        human_reviews_completed=sum(count == 55 for count in completed_columns.values()),
        identity_sets={key: sorted(values) for key, values in identities.items()}, identity_verified=False,
        content_review_passed=content_passed, release_gates_passed=gates_passed,
        all_55_items_resolved=all_resolved, all_55_items_passed=all_passed,
        approved_for_experiment=False, formal_eligible=False, release_blockers=blockers,
        source_unchanged=True,
        verification_scope="local_structure_identity_format_and_declared_reviews_only_not_real_human_authentication",
        counter_definitions={
            "completed_human_slots": "具有完整合法填写的reviewer_1/2槽数；不含裁决槽，不是已认证真人数。",
            "completed_items": "双槽均完成且一致，或分歧已独立裁决的条目数；包含需改与拒绝。",
            "disagreement_items": "双槽决定或检查值有分歧的条目数，含已裁决条目。",
            "human_reviews_completed": "在全部55项上完成填写的角色列数0..2，不是独立人员数或真实性认证。",
            "passing_checks": "按本版本硬门槛，approved/supported/acceptable必须所有检查值均为True；不自动反转问句极性。",
        })
    _reject_sensitive_keys(result)
    return result


@_review_api
def verify_human_review_submission(submission, *, evidence_build, live_semantic_path) -> dict:
    """重建权威空白模板并核验一个完整合并件（dict或项目内JSON路径）。

    仅reviewer槽、adjudication、已有计数与顶层衍生摘要可变；其他55项内容必须
    原样保留。待审槽允许合法的部分填写，但不计完成。已完成槽必须有非机器ID、
    带时区ISO时间、非空notes及完整bool检查值。调用不写文件、不认证真实身份。
    counts/status等客户端摘要全部重算；返回件可再次传入本API，无需修改旧模板。
    检查值有分歧时，第三人completed裁决还须附全键bool的checks；若仅决定不同
    而检查一致，可省略裁决checks并沿用一致值。原审查意见永不被裁决覆盖。
    """
    authority = _review_authority(evidence_build, live_semantic_path)
    files = {}
    document = _review_load(submission, files)
    result = _review_evaluate(document, authority)
    _review_finish(authority, files)
    return result


@_review_api
def merge_and_verify_human_reviews(template_or_path, reviewer_1_copy, reviewer_2_copy,
                                   adjudication_copy=None, *, evidence_build, live_semantic_path) -> dict:
    """合并两份独立全模板副本，返回核验后的完整文档；绝不保存或签署。

    每份副本只能填写对应的一列，另一列和裁决须与空白模板完全一致。
    adjudication_copy也为完整模板副本：可保留两列全空，或逐字保留本次两份
    审核副本合成的两列（便于第三人阅读）；不允许改写任一原审核意见。
    全局两审核角色ID集合互斥，第三人集合也不得与任一审核角色集合重叠。
    ID仅做NFKC+casefold+strip格式独立性核验，不构成真人认证或电子签名。
    """
    authority = _review_authority(evidence_build, live_semantic_path)
    files = {}
    template = _review_load(template_or_path, files)
    _require(_equal(template, authority), "source_template_not_blank_or_changed")
    copies = [_review_load(reviewer_1_copy, files), _review_load(reviewer_2_copy, files)]
    merged = deepcopy(authority)
    for slot, copy in zip(_REVIEW_SLOTS, copies):
        _review_shape(copy, authority)
        opposite = "reviewer_2" if slot == "reviewer_1" else "reviewer_1"
        for group in _GROUPS:
            for item, blank, target in zip(copy[group], authority[group], merged[group]):
                _require(_equal(item[opposite], blank[opposite])
                         and _equal(item["adjudication"], blank["adjudication"]), "copy_slot_ownership_violation")
                target[slot] = deepcopy(item[slot])
    # 先核对双人填写与全局独立性，再接纳第三人副本。
    _review_evaluate(merged, authority)
    if adjudication_copy is not None:
        decisions = _review_load(adjudication_copy, files)
        _review_shape(decisions, authority)
        matches_blank, matches_merged = True, True
        for group in _GROUPS:
            for item, blank, target in zip(decisions[group], authority[group], merged[group]):
                for slot in _REVIEW_SLOTS:
                    matches_blank = matches_blank and _equal(item[slot], blank[slot])
                    matches_merged = matches_merged and _equal(item[slot], target[slot])
                target["adjudication"] = deepcopy(item["adjudication"])
        _require(matches_blank or matches_merged, "adjudication_overwrites_reviewers")
    result = _review_evaluate(merged, authority)
    _review_finish(authority, files)
    return result
