"""候选剂量稿构建与独立验证。只离线装配，不请求模型、不签署人工审批。"""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import uuid

from .dosage import DISPLAY_POLICY, compact_evidence, make_dose_pair, public_payload
from .evidence import verify_bundle, render_evidence_block
from .runtime import canonical_hash, write_json_new, _parse_json, utc_now

SHORT_PUBLISHERS = {
    "CL_S01": "NOAA科普", "CL_S02": "NASA科普", "CL_S03": "NOAA科普", "CL_S04": "NASA研究介绍",
    "EN_S02": "IEA报告摘要", "EN_S05": "ILO联合报告", "EN_S06": "Nature通讯研究",
    "EN_S07": "PV杂志转引", "EN_S08": "北大能源院转引",
    "WE_S01": "中国气象局答复", "WE_S02": "中国气象局科普", "WE_S04": "中国气象报社",
    "WE_S06": "中国气象局历史答复",
}
MODEL_NOTICE = "AI辅助编辑设计稿；非MiMo四阶段或个性化镜像结果。"


def read_json(path):
    return _parse_json(Path(path).read_bytes())


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_index_from_review(evidence_build):
    build = Path(evidence_build)
    manifest = read_json(build / "manifest.json")
    record = next(row for row in manifest["files"] if row["path"] == "review/sources.json")
    if file_sha(build / record["path"]) != record["sha256"]:
        raise ValueError("来源审阅记录与上版冻结清单不一致")
    sources = read_json(build / "review/sources.json")["sources"]
    if len({row["source_id"] for row in sources}) != len(sources):
        raise ValueError("来源ID重复")
    if set(SHORT_PUBLISHERS) != {row["source_id"] for row in sources}:
        raise ValueError("新来源需要单独指定短显示名并升版")
    return {row["source_id"]: {"source_id": row["source_id"], "publisher_short": SHORT_PUBLISHERS[row["source_id"]],
            "publication_date": row["publication_date"], "source_url": row["url"],
            "source_family_id": row["source_family_id"]} for row in sources}


def validate_drafts(drafts, packets):
    if (drafts.get("schema_version") != "1.0" or drafts.get("version") != "dose_editorial_v1"
            or drafts.get("content_origin") != "editorial_design_draft"
            or drafts.get("not_model_output") is not True or drafts.get("natural_view_used") is not False
            or type(drafts.get("human_reviews_completed")) is not int or drafts["human_reviews_completed"] != 0):
        raise ValueError("草稿身份或版本不符合要求")
    items = drafts.get("pairs")
    if type(items) is not list or len(items) != len(packets):
        raise ValueError("需要完整六对草稿")
    seen = set()
    for item in items:
        key = (item.get("topic_id"), item.get("round"))
        if type(key[1]) is not int or key not in packets or key in seen:
            raise ValueError("轮次缺失、重复或无效")
        seen.add(key)
        packet = packets[key]
        if item["packet_id"] != packet["packet_id"]:
            raise ValueError("草稿绑定的证据包不一致")
        evidence_ids = {row["evidence_id"] for row in packet["evidence"]}
        for arm in ("I4", "I5"):
            citations = item.get(arm + "_citations")
            if (type(citations) is not list or any(type(x) is not str for x in citations)
                    or len(citations) != len(set(citations)) or not 2 <= len(citations) <= len(evidence_ids)
                    or not set(citations) <= evidence_ids):
                raise ValueError("附加段声明引用有误")
            if type(item.get(arm)) is not str or not item[arm].strip():
                raise ValueError("附加段不得缺失")
            actual = set(re.findall(r"\b(?:CL|EN|WE)_E\d+\b", item[arm]))
            if actual != set(citations):
                raise ValueError("正文引用与声明不一致或跨轮新增证据")
        if "？" in item["I5"] or "?" in item["I5"]:
            raise ValueError("普通复述不添加独有问题或回应机会")
    return True


def _packets(build):
    manifest = read_json(build / "manifest.json")
    packets = {}
    for topic in manifest["topics"]:
        for round_no in (1, 2):
            packets[topic["topic_id"], round_no] = read_json(build / topic[f"round_{round_no}_packet"])
    return packets


def create_dose_build(root, evidence_build, drafts_path):
    root, evidence_build, drafts_path = Path(root).resolve(), Path(evidence_build).resolve(), Path(drafts_path).resolve()
    verified = verify_bundle(evidence_build)
    packets = _packets(evidence_build)
    source_index = source_index_from_review(evidence_build)
    drafts = read_json(drafts_path)
    validate_drafts(drafts, packets)
    parent = root / "experiment/dose_candidates"
    if not parent.is_dir():
        raise FileNotFoundError(parent)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = parent / ("dose_" + stamp + "_" + uuid.uuid4().hex[:6])
    out.mkdir()
    for name in ("pairs", "payloads", "inputs", "review"):
        (out / name).mkdir()
    frozen_files = []
    def save(relative, value):
        path = out / relative
        write_json_new(path, value)
        frozen_files.append({"path": relative, "sha256": file_sha(path), "bytes": path.stat().st_size})
    save("inputs/additions.json", drafts)
    save("inputs/source_index.json", source_index)
    save("inputs/display_policy.json", DISPLAY_POLICY)
    audit_path = root / "experiment/dose_candidates/semantic_review_v1.json"
    audit = read_json(audit_path)
    if (audit.get("status") != "pass_with_limitations" or audit.get("input_sha256") != file_sha(drafts_path)
            or audit.get("review_type") != "independent_ai_not_human"
            or audit.get("content_origin") != "editorial_design_draft"
            or audit.get("human_reviews_completed") != 0 or audit.get("approved_for_experiment") is not False):
        raise ValueError("附加段尚未通过绑定当前版本的独立AI复核")
    save("review/semantic_audit.json", audit)
    rows = []
    provenance = {"content_origin": "editorial_design_draft", "natural_view_used": False, "source_event_ids": []}
    for draft in sorted(drafts["pairs"], key=lambda row: (row["topic_id"], row["round"])):
        key = draft["topic_id"], draft["round"]
        packet = packets[key]
        pair_id = f"{key[0]}_R{key[1]}"
        save(f"inputs/{pair_id}_packet.json", packet)
        pair = make_dose_pair(packet, source_index, draft["I4"], draft["I5"], provenance=provenance)
        save(f"pairs/{pair_id}.json", pair)
        for arm in ("I4", "I5"):
            payload = public_payload(pair, arm)
            save(f"payloads/{pair_id}_{arm}.json", payload)
        compact = compact_evidence(packet, source_index)
        row = {"pair_id": pair_id, "topic_id": key[0], "round": key[1], "packet_id": packet["packet_id"],
               "pair_file": f"pairs/{pair_id}.json", "packet_file": f"inputs/{pair_id}_packet.json",
               "payload_files": {arm: f"payloads/{pair_id}_{arm}.json" for arm in ("I4", "I5")},
               "evidence_ids": pair["I4"]["evidence_ids"], "packet_sha256": canonical_hash(packet),
               "old_evidence_block_chars": len(render_evidence_block(packet)),
               "new_evidence_block_chars": compact["char_count"],
               "I4_visible_chars": pair["I4"]["char_count"], "I5_visible_chars": pair["I5"]["char_count"],
               "I4_additional_chars": pair["I4"]["additional_chars"], "I5_additional_chars": pair["I5"]["additional_chars"],
               "total_difference_ratio": pair["checks"]["total_difference_ratio"],
               "additional_difference_ratio": pair["checks"]["additional_difference_ratio"],
               "assembly_valid": pair["checks"]["assembly_valid"], "deviations": pair["checks"]["deviations"],
               "pair_sha256": canonical_hash(pair)}
        rows.append(row)
    topic_totals = [{"topic_id": tid,
                     "I4_total_chars": sum(row["I4_visible_chars"] for row in rows if row["topic_id"] == tid),
                     "I5_total_chars": sum(row["I5_visible_chars"] for row in rows if row["topic_id"] == tid)}
                    for tid in sorted({row["topic_id"] for row in rows})]
    save("review/human_signoff.json", {"scope": "candidate_display_and_additions", "reviewer_1": None,
           "reviewer_2": None, "reviewer_1_findings": None, "reviewer_2_findings": None,
           "adjudication": None, "approved_for_experiment": False,
           "do_not_auto_fill": True, "required": ["正文及来源完整一致", "附加段不含新增事实",
                 "同证据两个角色的语义区别", "新显示口径适用且来源可追溯", "真实T1观点与镜像接入另验"]})
    summary = {"schema_version": "1.0", "build_id": out.name, "built_at_utc": utc_now(),
               "status": "dose_draft_passed" if all(row["assembly_valid"] for row in rows) else "dose_draft_with_deviations",
               "content_origin": "editorial_design_draft", "notice": MODEL_NOTICE,
               "evidence_build_directory": str(evidence_build), "evidence_manifest_sha256": file_sha(evidence_build / "manifest.json"),
               "input_drafts_path": str(drafts_path), "input_drafts_sha256": file_sha(drafts_path),
               "topic_count": verified["topic_count"], "distinct_evidence_count": verified["evidence_count"],
               "pair_count": len(rows), "payload_count": len(rows) * 2,
               "display_version": DISPLAY_POLICY["version"], "display_policy_sha256": canonical_hash(DISPLAY_POLICY),
               "pairs": rows, "topic_totals": topic_totals, "human_reviews_completed": 0,
               "source_text_changed": False, "source_url_changed": False,
               "links_in_subject_context": False, "extra_browsing_actions": False,
               "model_calls_this_build": 0, "natural_view_used": False, "personalized_mirror_completed": False,
               "formal_eligible": False, "approved_for_experiment": False,
               "draft_is_not_full_I4": True,
               "counting_policy": "实际payload.messages[0].content的全部Python len；含标签标点空白回应提示，不以汉字数代替。URL和家族映射不在该消息，禁止隐式追加或提供浏览动作。",
               "change_note": "从完整URL/家族ID内联改为来源ID+机构短名+文献日期内联，所有证据正文不变。原始链接供审核；若未来模型也接收链接，必须整体重计并升版。不是对旧输入只隐藏显示。",
               "remaining": ["双人独立人工复核和权利确认", "新显示规则须在正式实验前注册",
                             "T1自然观点与真实四阶段报告/个体镜像", "真实I4/I5附加段生成及逐项引用审计",
                             "候选立场轴的适用性", "受试测量、OASIS、108卡库存与金标准均另验"],
               "files": frozen_files}
    write_json_new(out / "manifest.json", summary)
    result = verify_dose_build(out)
    write_json_new(out / "verification.json", result)
    return {"build_directory": str(out), "summary": summary, "verification": result}


def _contained(root, relative):
    if type(relative) is not str or Path(relative).is_absolute():
        raise ValueError("构建文件须为相对路径")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("构建引用越界")
    return path


def verify_dose_build(out):
    try:
        return _verify_dose_build(out)
    except (KeyError, TypeError, IndexError, StopIteration) as exc:
        raise ValueError("构建清单结构不完整或含无效类型") from exc


def _verify_dose_build(out):
    out = Path(out).resolve()
    manifest = read_json(out / "manifest.json")
    frozen_claims = {"schema_version": "1.0", "content_origin": "editorial_design_draft",
                     "approved_for_experiment": False, "formal_eligible": False,
                     "human_reviews_completed": 0, "natural_view_used": False,
                     "personalized_mirror_completed": False, "draft_is_not_full_I4": True,
                     "source_text_changed": False, "source_url_changed": False,
                     "links_in_subject_context": False, "extra_browsing_actions": False,
                     "model_calls_this_build": 0, "display_version": DISPLAY_POLICY["version"],
                     "notice": MODEL_NOTICE}
    for key, expected in frozen_claims.items():
        if type(manifest.get(key)) is not type(expected) or manifest[key] != expected:
            raise ValueError("不允许改变候选稿身份、展示或正式资格声明")
    if read_json(out / "inputs/display_policy.json") != DISPLAY_POLICY or manifest["display_policy_sha256"] != canonical_hash(DISPLAY_POLICY):
        raise ValueError("显示版本不匹配")
    names = [row["path"] for row in manifest["files"]]
    if len(names) != len(set(names)):
        raise ValueError("重复清单文件")
    for row in manifest["files"]:
        path = _contained(out, row["path"])
        if file_sha(path) != row["sha256"] or path.stat().st_size != row["bytes"]:
            raise ValueError("构建文件哈希不匹配")
    evidence_build = Path(manifest["evidence_build_directory"])
    if file_sha(evidence_build / "manifest.json") != manifest["evidence_manifest_sha256"]:
        raise ValueError("原候选包清单变动")
    original_verification = verify_bundle(evidence_build)
    original_packets = _packets(evidence_build)
    source_index = read_json(out / "inputs/source_index.json")
    if source_index != source_index_from_review(evidence_build):
        raise ValueError("短来源表与原始链接、文献日期或家族不一致")
    drafts = read_json(out / "inputs/additions.json")
    validate_drafts(drafts, original_packets)
    if type(manifest["pairs"]) is not list or len(manifest["pairs"]) != len(original_packets):
        raise ValueError("六对轮次不完整或重复")
    keys = [(row["topic_id"], row["round"]) for row in manifest["pairs"]]
    if (any(type(key[1]) is not int for key in keys) or len(set(keys)) != len(keys)
            or set(keys) != set(original_packets)):
        raise ValueError("实际议题轮次不完整、重复或无效")
    required_files = {"inputs/additions.json", "inputs/source_index.json", "inputs/display_policy.json",
                      "review/human_signoff.json", "review/semantic_audit.json"}
    for key in original_packets:
        pair_id = f"{key[0]}_R{key[1]}"
        required_files.update({f"inputs/{pair_id}_packet.json", f"pairs/{pair_id}.json",
                               f"payloads/{pair_id}_I4.json", f"payloads/{pair_id}_I5.json"})
    if set(names) != required_files:
        raise ValueError("清单必须覆盖且只包含所有冻结输入、配对和消息文件")
    audit = read_json(out / "review/semantic_audit.json")
    if (audit.get("status") != "pass_with_limitations" or audit.get("input_sha256") != manifest["input_drafts_sha256"]
            or audit.get("review_type") != "independent_ai_not_human" or audit.get("checked_pairs") != len(original_packets)
            or audit.get("human_reviews_completed") != 0 or audit.get("approved_for_experiment") is not False
            or {row["packet_id"] for row in audit["checks"]} != {p["packet_id"] for p in original_packets.values()}
            or any(row.get("I4_supported") is not True or row.get("I5_supported") is not True for row in audit["checks"])):
        raise ValueError("独立复核结果未绑定完整候选稿")
    # 原草稿字节hash绑定的是外部原版，不以重新排版的内部副本字节替代。
    if file_sha(manifest["input_drafts_path"]) != manifest["input_drafts_sha256"] or read_json(manifest["input_drafts_path"]) != drafts:
        raise ValueError("冻结的附加草稿或外部版本已变动")
    signoff = read_json(out / "review/human_signoff.json")
    if signoff.get("approved_for_experiment") is not False or any(signoff.get(field) is not None for field in
            ("reviewer_1", "reviewer_2", "reviewer_1_findings", "reviewer_2_findings", "adjudication")):
        raise ValueError("候选构建不能自动填写人工审核")
    outcomes = []
    for row in manifest["pairs"]:
        key = row["topic_id"], row["round"]
        expected_id = f"{key[0]}_R{key[1]}"
        if (row["pair_id"] != expected_id or row["packet_id"] != original_packets[key]["packet_id"]
                or row["pair_file"] != f"pairs/{expected_id}.json" or row["packet_file"] != f"inputs/{expected_id}_packet.json"
                or row["payload_files"] != {arm: f"payloads/{expected_id}_{arm}.json" for arm in ("I4", "I5")}):
            raise ValueError("pair_id、文件名与实际议题轮次不匹配")
        packet = read_json(_contained(out, row["packet_file"]))
        if packet != original_packets[key] or canonical_hash(packet) != row["packet_sha256"]:
            raise ValueError("证据包内容变动")
        pair = read_json(_contained(out, row["pair_file"]))
        draft = next(item for item in drafts["pairs"] if (item["topic_id"], item["round"]) == key)
        expected = make_dose_pair(packet, source_index, draft["I4"], draft["I5"], provenance={
            "content_origin": "editorial_design_draft", "natural_view_used": False, "source_event_ids": []})
        if pair != expected or canonical_hash(pair) != row["pair_sha256"]:
            raise ValueError("装配结果不等于独立重建结果")
        for arm in ("I4", "I5"):
            payload = read_json(_contained(out, row["payload_files"][arm]))
            if payload != public_payload(pair, arm):
                raise ValueError("实际待发送payload不匹配")
            if len(payload["messages"][0]["content"]) != row[arm + "_visible_chars"]:
                raise ValueError("计字与实际消息不同")
        counts = {"old_evidence_block_chars": len(render_evidence_block(packet)),
                  "new_evidence_block_chars": len(pair["I4"]["evidence_block"]),
                  "I4_additional_chars": pair["I4"]["additional_chars"],
                  "I5_additional_chars": pair["I5"]["additional_chars"],
                  "evidence_ids": pair["I4"]["evidence_ids"]}
        for metric, value in counts.items():
            if type(row[metric]) is not type(value) or row[metric] != value:
                raise ValueError("摘要字符统计或证据列表不真实")
        for metric in ("total_difference_ratio", "additional_difference_ratio", "assembly_valid", "deviations"):
            if type(row[metric]) is not type(pair["checks"][metric]) or row[metric] != pair["checks"][metric]:
                raise ValueError("摘要与实际校验不一致")
        outcomes.append(pair["checks"]["assembly_valid"])
    totals = [{"topic_id": tid,
               "I4_total_chars": sum(row["I4_visible_chars"] for row in manifest["pairs"] if row["topic_id"] == tid),
               "I5_total_chars": sum(row["I5_visible_chars"] for row in manifest["pairs"] if row["topic_id"] == tid)}
               for tid in sorted({key[0] for key in original_packets})]
    if canonical_hash(manifest["topic_totals"]) != canonical_hash(totals):
        raise ValueError("两轮累计统计与实际消息不同")
    aggregates = {"topic_count": original_verification["topic_count"],
                  "distinct_evidence_count": original_verification["evidence_count"],
                  "pair_count": len(outcomes), "payload_count": 2 * len(outcomes)}
    for key, value in aggregates.items():
        if type(manifest[key]) is not int or manifest[key] != value:
            raise ValueError("构建汇总数量不匹配")
    expected_status = "dose_draft_passed" if all(outcomes) else "dose_draft_with_deviations"
    if manifest["status"] != expected_status:
        raise ValueError("摘要成功状态与执行结果不一致")
    return {"integrity_valid": True, "dose_draft_valid": all(outcomes), "pairs_checked": len(outcomes),
            "pairs_passed": sum(outcomes), "source_text_unchanged": True, "source_map_unchanged": True,
            "all_subject_payloads_rebuilt": True, "formal_eligible": False, "human_reviews_completed": 0}


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--evidence-build", type=Path)
    parser.add_argument("--drafts", type=Path)
    parser.add_argument("--build-dir", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        if args.build_dir is None:
            parser.error("verify需要--build-dir")
        result = verify_dose_build(args.build_dir)
    else:
        if args.evidence_build is None or args.drafts is None:
            parser.error("build需要--evidence-build和--drafts")
        built = create_dose_build(args.root, args.evidence_build, args.drafts)
        result = {"build_directory": built["build_directory"], **built["verification"],
                  "lengths": [{k: row[k] for k in ("pair_id", "old_evidence_block_chars", "new_evidence_block_chars", "I4_visible_chars", "I5_visible_chars", "additional_difference_ratio")} for row in built["summary"]["pairs"]]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
