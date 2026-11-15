"""从已取证的来源建立候选证据包。纯离线；结构通过不提升为正式人工批准。"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid
import zipfile

from .contracts import validate_packet
from .runtime import canonical_hash, utc_now, write_json_new, _parse_json

TOPICS = ("climate", "energy", "weather")
PROTOCOL_SHA = "3d3ba6f0e21237cb288fe4a9558ab015a09b56f1a9538af81e5c9075637f6253"
SOURCE_LEVELS = {
    "CL_S01": "scientific_explainer", "CL_S02": "scientific_explainer",
    "CL_S03": "scientific_explainer", "CL_S04": "research_explainer",
    "EN_S02": "first_party_report_summary", "EN_S05": "first_party_joint_report_release",
    "EN_S06": "original_research", "EN_S07": "secondary_report", "EN_S08": "secondary_report",
    "WE_S01": "official_historical_response", "WE_S02": "official_historical_standard_explainer",
    "WE_S04": "science_explainer", "WE_S06": "official_historical_response",
}


def _read(path):
    return _parse_json(Path(path).read_bytes())


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _inside(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("来源引用越出指定取证目录")
    return path


def _quote_count(text, language):
    if language == "en":
        return len(re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", text))
    return len(text)


def collect_materials(source_root):
    """核对全部入选来源文件和引文；仅采用本机已有的成功HTTP响应。"""
    root = Path(source_root).resolve()
    materials = []
    all_ids = set()
    sources_by_id = {}
    source_checks, evidence_checks = [], []
    for folder in TOPICS:
        base = root / "intake" / folder
        material = _read(base / "topic_material.json")
        if material.get("human_reviews_completed") != 0 or material.get("review_status") != "pending_dual_human_review":
            raise ValueError("候选构建不允许自动认定人工批准")
        if len(material["dimensions"]) != 6 or len(set(material["dimensions"])) != 6:
            raise ValueError("须列出六个候选证据维度")
        text_by_source = {}
        local_sources = {}
        for source in material["sources"]:
            sid = source["source_id"]
            if sid in sources_by_id:
                raise ValueError("来源ID重复")
            meta_path = _inside(base, base / source["meta_file"])
            meta = _read(meta_path)
            if (meta["source_id"] != sid or meta["outcome"] != "downloaded_text_extracted"
                    or meta["http_status"] != 200 or meta["process_returncode"] != 0):
                raise ValueError("入选来源缺少成功取证记录")
            raw_path = _inside(base, meta["raw_path"])
            text_path = _inside(base, meta["text_path"])
            raw = raw_path.read_bytes()
            text_data = _read(text_path)
            text = text_data["text"]
            if (_sha(raw) != meta["raw_sha256"] or len(raw) != meta["bytes"]
                    or _sha(text.encode("utf-8")) != meta["text_sha256"]
                    or text_data["source_id"] != sid or len(text) != meta["text_char_count"]):
                raise ValueError("来源字节或提取文本哈希不一致")
            if source["url"] != meta["requested_url"]:
                raise ValueError("材料URL与实际请求URL不匹配")
            datetime.fromisoformat(meta["completed_at_utc"].replace("Z", "+00:00"))
            if source["language"] not in ("en", "zh"):
                raise ValueError("来源语言不支持")
            for name in ("title", "publisher", "source_family_id", "license_note"):
                if not isinstance(source.get(name), str) or not source[name]:
                    raise ValueError("来源元数据不完整")
            record = {**deepcopy(source), "source_level": SOURCE_LEVELS[sid],
                      "retrieved_at_utc": meta["completed_at_utc"], "raw_sha256": meta["raw_sha256"],
                      "text_sha256": meta["text_sha256"], "effective_url": meta["effective_url"],
                      "intake_meta": str(meta_path.relative_to(root)),
                      "intake_raw": str(raw_path.relative_to(root)),
                      "intake_text": str(text_path.relative_to(root)),
                      "redistribution_cleared": False}
            source_checks.append({"source_id": sid, "http_status": 200, "raw_hash_valid": True,
                                  "extracted_text_hash_valid": True, "bytes": len(raw)})
            local_sources[sid] = sources_by_id[sid] = record
            text_by_source[sid] = text
        quotes = {}
        material["source_records"] = local_sources
        material["material_file"] = str((base / "topic_material.json").relative_to(root))
        material["material_sha256"] = _sha((base / "topic_material.json").read_bytes())
        for entry in material["evidence"]:
            eid, sid = entry["evidence_id"], entry["source_id"]
            if eid in all_ids or sid not in local_sources:
                raise ValueError("证据ID重复或来源不存在")
            all_ids.add(eid)
            quote = entry["original_quote"]
            if not quote or quote not in text_by_source[sid]:
                raise ValueError("引文不是取证文本的精确连续片段")
            if entry["transformation_type"] not in ("translated_adapted", "adapted"):
                raise ValueError("转换类型必须如实注明")
            if entry["review_dimension"] not in material["dimensions"]:
                raise ValueError("候选维度映射不匹配")
            quotes.setdefault(sid, []).append(quote)
            entry["quote_start"] = text_by_source[sid].index(quote)
            entry["quote_end"] = entry["quote_start"] + len(quote)
            entry["content_sha256"] = _sha(entry["text"].encode("utf-8"))
            evidence_checks.append({"evidence_id": eid, "source_id": sid, "quote_exact_match": True,
                                    "quote_start": entry["quote_start"], "quote_end": entry["quote_end"],
                                    "content_sha256": entry["content_sha256"], "text_characters": len(entry["text"]),
                                    "transformation_type": entry["transformation_type"]})
        for sid, parts in quotes.items():
            count = _quote_count(" ".join(parts), local_sources[sid]["language"])
            limit = 180 if local_sources[sid]["language"] == "en" else 300
            if count > limit:
                raise ValueError("同一来源短引文超过本轮限制")
            local_sources[sid]["direct_quote_units"] = count
            local_sources[sid]["direct_quote_limit"] = limit
        materials.append(material)
    return materials, sources_by_id, source_checks, evidence_checks


def public_packet(material, evidence=None, suffix="FULL"):
    items = material["evidence"] if evidence is None else evidence
    rows = []
    for item in items:
        source = material["source_records"][item["source_id"]]
        rows.append({"evidence_id": item["evidence_id"], "text": item["text"],
                     "source_url": source["url"], "source_family_id": source["source_family_id"],
                     "content_sha256": _sha(item["text"].encode("utf-8"))})
    packet = {"schema_version": "1.0", "packet_id": f"CAND_{material['topic_id']}_{suffix}_V1",
              "topic_id": material["topic_id"], "topic": material["candidate_title"],
              "stance_axis": deepcopy(material["stance_axis"]), "evidence": rows, "is_demo": False}
    return validate_packet(packet, allow_demo=False)


def render_evidence_block(packet):
    # 与runtime.make_intervention_pair的证据块渲染相同；本函数不生成任何I4/I5附加文字。
    return "\n\n".join(f"[证据 {item['evidence_id']}]\n{item['text']}\n来源：{item['source_url']}\n来源族：{item['source_family_id']}"
                        for item in packet["evidence"])


def verify_bundle(out):
    out = Path(out).resolve()
    manifest = _read(out / "manifest.json")
    if manifest.get("status") != "candidate_pending_human_review" or manifest.get("approved_for_experiment") is not False:
        raise ValueError("候选资格被改变")
    for entry in manifest["files"]:
        file = _inside(out, out / entry["path"])
        if _sha(file.read_bytes()) != entry["sha256"]:
            raise ValueError("导出文件哈希不一致")
    count = 0
    for topic in manifest["topics"]:
        for field in ("full_packet", "round_1_packet", "round_2_packet"):
            packet = validate_packet(_read(_inside(out, out / topic[field])), allow_demo=False)
            if packet["topic_id"] != topic["topic_id"]:
                raise ValueError("导出议题ID不匹配")
            count += 1
        full = _read(out / topic["full_packet"])
        first = _read(out / topic["round_1_packet"])
        second = _read(out / topic["round_2_packet"])
        if first["evidence"] + second["evidence"] != full["evidence"]:
            raise ValueError("两轮候选切分与完整证据库不一致")
    return {"passed": True, "packet_count": count, "file_count": len(manifest["files"]),
            "topic_count": len(manifest["topics"]), "evidence_count": manifest["evidence_count"],
            "approved_for_experiment": False, "human_reviews_completed": 0}


def build_bundle(source_root, protocol_path):
    root = Path(source_root).resolve()
    protocol_path = Path(protocol_path)
    if _sha(protocol_path.read_bytes()) != PROTOCOL_SHA:
        raise ValueError("原方案版本与已核对版本不一致")
    materials, sources, source_checks, evidence_checks = collect_materials(root)
    followup = _read(root / "review/independent_ai_followup.json")
    if followup.get("status") != "pass_with_limitations" or followup.get("human_review") is not False:
        raise ValueError("独立AI复核修订尚未闭环")
    bindings = {row["topic"]: row for row in followup["topic_file_bindings"]}
    if set(bindings) != set(TOPICS):
        raise ValueError("复核未覆盖全部候选议题")
    for folder, material in zip(TOPICS, materials):
        if bindings[folder]["byte_sha256"] != material["material_sha256"]:
            raise ValueError("素材已在复核后变更，必须重新复核")
    if _sha((root / "review/independent_ai_audit.json").read_bytes()) != followup["prior_review"]["byte_sha256"]:
        raise ValueError("初次审计已变更")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = root / ("build_" + stamp + "_" + uuid.uuid4().hex[:6])
    out.mkdir()
    for name in ("packets", "review"):
        (out / name).mkdir()
    file_paths = []
    def save(relative, data):
        path = out / relative
        write_json_new(path, data)
        file_paths.append(path)
    topic_index, review_rows = [], []
    for material in materials:
        topic_id = material["topic_id"]
        full = public_packet(material)
        split = (len(material["evidence"]) + 1) // 2
        packets = [full, public_packet(material, material["evidence"][:split], "R1"),
                        public_packet(material, material["evidence"][split:], "R2")]
        names = [f"packets/{topic_id}_{suffix}.json" for suffix in ("full", "round_1", "round_2")]
        for name, packet in zip(names, packets):
            save(name, packet)
        dose = []
        for packet in packets[1:]:
            size = len(render_evidence_block(packet))
            dose.append({"packet_id": packet["packet_id"], "evidence_count": len(packet["evidence"]),
                         "evidence_block_characters": size, "characters_remaining_to_800": max(0, 800 - size - 2),
                         "evidence_alone_exceeds_800": size > 800,
                         "planned_total_min": 600, "planned_total_max": 800,
                         "actual_I4_or_I5_delivery": False,
                         "note": "仅按现有渲染器估算；未含助手/对照附加文字，不能当作剂量通过。"})
        topic_index.append({"topic_id": topic_id, "candidate_title": material["candidate_title"],
                            "evidence_count": len(full["evidence"]), "source_count": len(material["sources"]),
                            "source_family_count": len({s["source_family_id"] for s in material["sources"]}),
                            "full_packet": names[0], "round_1_packet": names[1], "round_2_packet": names[2],
                            "full_packet_sha256": canonical_hash(full), "round_dose_diagnostics": dose,
                            "source_material": material["material_file"], "source_material_sha256": material["material_sha256"],
                            "scope_note": material["scope_note"], "time_scope": material["time_scope"],
                            "U_or_polarization_eligible": False,
                            "axis_status": "compatible_emphases_not_validated_bipolar_scale",
                            "approved_for_experiment": False})
        for item in material["evidence"]:
            source = material["source_records"][item["source_id"]]
            review_rows.append({**deepcopy(item), "topic_id": topic_id, "source": deepcopy(source),
                                "review_status": "pending_dual_human_review",
                                "human_review_1": None, "human_review_2": None,
                                "human_reviews_completed": 0, "adjudication": None,
                                "required_checks": ["原文支持", "实体数字条件否定核对", "历史时间截面", "翻译释义", "使用及再分发权利"]})
    save("review/evidence_review.json", {"role": "reviewer_only_not_assistant_context",
          "items": review_rows, "contains_final_test_answers": False,
          "human_reviews_completed": 0, "approved_for_experiment": False})
    save("review/sources.json", {"sources": list(sources.values()),
          "raw_snapshots": "保存在来源目录intake，未纳入可下载包，不作公开再分发",
          "exclusions": [entry for material in materials for entry in material["exclusions"]]})
    save("review/ai_audit_initial.json", _read(root / "review/independent_ai_audit.json"))
    save("review/ai_audit_followup.json", followup)
    save("review/automation_checks.json", {"source_checks": source_checks, "evidence_checks": evidence_checks,
          "passed": True, "quote_match_is_not_semantic_proof": True,
          "role": "automated_validation_not_human_approval", "paid_model_calls": 0})
    manifest = {"schema_version": "1.0", "build_id": out.name, "built_at_utc": utc_now(),
                "status": "candidate_pending_human_review", "approved_for_experiment": False,
                "protocol_path": str(protocol_path.resolve()), "protocol_sha256": PROTOCOL_SHA,
                "protocol_sections": ["4.3", "5.2", "5.3", "5.4", "7.1", "7.2"],
                "source_root": str(root), "topics": topic_index, "evidence_count": len(review_rows),
                "selected_source_count": len(sources), "human_reviews_completed": 0,
                "truth_label_source": "none_no_gold_created", "paid_model_calls": 0,
                "is_demo_note": "packet.is_demo=false只表明基于真实来源，不代表人工批准或正式实验资格",
                "selection_policy": "原方案三个建议方向；天气消息为中文公共核查方向下的候选收窄，不冒充CHECKED原帖",
                "round_split_policy": "离线顺序切分供剂量检查，不是已按T1自然观点生成的个性化检索结果",
                "rights": "local_review_only_redistribution_not_cleared",
                "remaining": ["双人独立人工核对及裁决", "英文释义第二人复核", "逐项权利与用途确认",
                              "三议题轴及U适用性评估", "T1观点驱动的共同证据包规则",
                              "I4/I5真实完整送达与600-800字符剂量核查",
                              "每议题108张曝光保持材料、历史错误卡及独立锚题金标准另建"],
                "files": [{"path": str(path.relative_to(out)).replace("\\", "/"),
                           "sha256": _sha(path.read_bytes()), "bytes": path.stat().st_size} for path in file_paths]}
    write_json_new(out / "manifest.json", manifest)
    verified = verify_bundle(out)
    write_json_new(out / "verification.json", verified)
    return {"build_directory": str(out), "manifest": manifest, "verification": verified}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--build-directory", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        if not args.source_root or not args.protocol:
            parser.error("build需要--source-root及--protocol")
        result = build_bundle(args.source_root, args.protocol)
        print(json.dumps({"build_directory": result["build_directory"], **result["verification"]}, ensure_ascii=True, indent=2))
    else:
        if not args.build_directory:
            parser.error("verify需要--build-directory")
        print(json.dumps(verify_bundle(args.build_directory), ensure_ascii=True, indent=2))
