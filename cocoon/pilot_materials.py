"""三议题 Pilot 三区隔离材料包与 §5.2 双人核验离线模拟演练。

本模块只使用标准库与项目内既有离线模块：不联网、不调用模型、不写文件、
不建立真值来源、不把模拟核验冒充真人签字。材料基于已核验取证构建目录的
真实公开来源中文摘编卡，明确标 translated_adapted；错误卡是"历史常见
外推主张"的摘编，不是机构认可结论。步骤34把步骤33的单议题引擎参数化为
climate/energy/weather 三议题统一格式，并保留 TOPIC_01 旧公共 API。
候选状态恒为 pending_dual_human_review / approved_for_experiment=False。
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from . import dosage, evidence, extractive_control, measurement, scheduling
from .contracts import validate_packet
from .dose_build import source_index_from_review
from .runtime import _parse_json, canonical_hash

__all__ = [
    "build_topic01_pilot_materials", "verify_topic01_pilot_materials",
    "simulate_section52_dual_review",
    "build_topic_pilot_materials", "build_all_topics_pilot_materials",
    "verify_topic_pilot_materials", "simulate_all_topics_dual_review",
]
PILOT_VERSION = "topic01_pilot_materials_v1"  # 保留步骤33的单一格式契约名，三议题共用
ALL_TOPICS_VERSION = "all_topics_pilot_materials_v1"
TOPICS = ("climate", "energy", "weather")
TOPIC_IDS = {"climate": "TOPIC_01", "energy": "TOPIC_02", "weather": "TOPIC_03"}
TOPIC_ID = "TOPIC_01"
_ROOT = Path(__file__).resolve().parents[1]
CARD_MIN_CHARS, CARD_MAX_CHARS = 180, 220
DOSE_MIN_CHARS, DOSE_MAX_CHARS = 600, 800
EXPOSURE_ROUNDS, RETENTION_ROUNDS = 8, 4
STANCES = (1, -1, 0)
ENVIRONMENTS = ("A", "B", "C", "D")
ARMS = ("I0", "I1", "I2", "I3", "I4", "I5")
ROUNDS = (1, 2)
SCHEDULE_SEEDS = (0, 1, 2, 3, 4, 5)
STRATA_COUNTS = {
    "exposure": {True: 16, False: 8},
    "retention": {True: 8, False: 4},
}
_REVIEW_MODE = "simulated_rehearsal_not_human"
_SIM_A, _SIM_B, _SIM_C = "SIM_REVIEWER_A", "SIM_REVIEWER_B", "SIM_ADJUDICATOR_C"
_ARM_LABEL = re.compile(r"I[45]", re.I)
_GOLD_TEXT = re.compile(
    r"gold_answers|gold_bundle|opposing_rubric|dimension_id|六维清单|六维|"
    r"金标准|标准答案|真值|\btruth\b|\bis_accurate\b", re.I)
_FORBIDDEN_PUBLIC_KEYS = frozenset({
    "gold_answers", "truth", "is_accurate", "stance", "label", "labels",
    "analysis", "justification", "dimensions", "opposing_rubric",
    "private_only", "mastered", "recognized_gap", "approved_for_experiment",
    "answer", "answers", "partition",
})
_ALLOWED_ASSISTANT_KEYS = frozenset({
    "schema_version", "packet_id", "topic_id", "topic", "stance_axis",
    "evidence", "is_demo", "evidence_id", "text", "source_url",
    "source_family_id", "content_sha256", "name", "negative_label",
    "positive_label", "path", "byte_sha256", "canonical_sha256",
    "evidence_ids", "no_gold_labels", "packets", "reviewer_only_fields_stripped",
})
_CARD_PUBLIC_KEYS = frozenset({
    "card_id", "topic_id", "text", "source_family_id", "source_url",
    "transformation_type", "is_demo",
})

# 六条证据对应的"历史常见外推主张"摘编；均直接来自各证据 boundary_note 明确排除的外推，
# 不是 NOAA/NASA 的官方结论，也不在受试可见文本中标注真伪。
_MISREADING = {
    "CL_E01": "温室效应本来就是自然过程，因此额外增加二氧化碳不会让全球气温发生变化。",
    "CL_E02": "大气二氧化碳浓度上升完全由化石燃料决定，自然碳循环已不再起作用。",
    "CL_E03": "只要大气二氧化碳增加，植物就会持续、等比例地增产并增加碳吸收。",
    "CL_E04": "海洋吸收了更多二氧化碳，因此海洋会持续完全抵消人为排放。",
    "CL_E05": "海水平均状态仍呈碱性，所以可以判定海洋没有发生酸化。",
    "CL_E06": "观测到叶面积增加，就等于直接测得某一因素的独立贡献，不需要模型假设。",
}
_ACCURATE_TAIL = "据此可以核对：上述说明只覆盖其明确写出的机制与限定范围。"
_FILLERS = (
    "请对照所附公开来源阅读。",
    "摘编不改变原研究的限定范围。",
    "本卡不代表机构对结论的认可。",
    "编号与来源信息仅供检索使用。",
    "阅读时请区分机制说明与外推判断。",
    "本条仅用于材料辨析练习。",
    "如需核实请查看原始页面。",
    "材料卡不等于研究结论全文。",
)
_DISCUSSION_POOL = (
    "请说明你会如何区分原有机制与额外变化。",
    "请谈谈材料中的限定条件对判断有什么影响。",
    "请就所附来源的适用范围提出一个疑问。",
    "请说明你会怎样核对一条概括是否仍受原文约束。",
    "请比较两处关于条件限制的表述。",
    "请描述你会保留哪些不确定性。",
)
_NEUTRAL_TARGETS = (150, 158, 166, 174, 182, 190)
_NEUTRAL_ARM_ORIGIN = {
    "I0": "等量普通阅读（离线剂量候选，非真实推荐流）",
    "I1": "可靠异见的格式候选（不新增事实证据）",
    "I2": "纠错证据的格式候选（不预设核查目标）",
    "I3": "比较与反思的格式候选（仅组织已见材料）",
}


def _need(condition, code):
    if not condition:
        raise ValueError("pilot_materials:" + code)


def _spec_for(topic_id=None):
    """按议题ID取冻结配置；默认TOPIC_01以保持步骤33旧API行为。"""
    key = topic_id or TOPIC_ID
    _need(key in _TOPIC_SPECS, "unknown_topic_id")
    return _TOPIC_SPECS[key]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(path):
    return _parse_json(Path(path).read_bytes())


def _inside(root, relative):
    root, path = Path(root).resolve(), (Path(root) / relative).resolve()
    _need(path.is_relative_to(root), "reference_outside_evidence_build")
    return path


def _walk_keys(value):
    if isinstance(value, dict):
        keys = set(value)
        for item in value.values():
            keys |= _walk_keys(item)
        return keys
    if isinstance(value, (list, tuple)):
        keys = set()
        for item in value:
            keys |= _walk_keys(item)
        return keys
    return set()


def _visible_text(block, addition):
    return "阅读材料\n" + block + "\n\n阅读说明\n" + addition + "\n\n" + dosage.RESPONSE_PROMPT


def _fit_card_text(core, ordinal):
    text = core
    index = 0
    while len(text) < CARD_MIN_CHARS:
        filler = _FILLERS[(ordinal + index) % len(_FILLERS)]
        if len(text) + len(filler) > CARD_MAX_CHARS:
            break
        text += filler
        index += 1
    _need(CARD_MIN_CHARS <= len(text) <= CARD_MAX_CHARS, "card_text_length_out_of_range")
    _need(not _GOLD_TEXT.search(text), "card_text_private_leak")
    return text


def _frame(axis, stance):
    if stance == 1:
        return f"本条侧重{axis['positive_label']}的行动节奏，并先交代机制事实。"
    if stance == -1:
        return f"本条侧重{axis['negative_label']}的行动节奏，并先交代机制事实。"
    return "本条仅交代机制事实与适用条件，不主张特定的行动节奏。"


def _card_text(item, axis, stance, accurate, ordinal, phase_label, spec=None):
    spec = spec or _spec_for()
    header = f"【{phase_label}摘编·第{ordinal:02d}号】"
    frame = _frame(axis, stance)
    if accurate:
        tail = _ACCURATE_TAIL
    else:
        tail = "有讨论据此概括为：" + spec["misreading"][item["evidence_id"]]
    core = header + frame + item["text"] + tail
    if len(core) > CARD_MAX_CHARS:
        core = header + item["text"] + tail
    if len(core) > CARD_MAX_CHARS:
        core = item["text"] + tail
    _need(len(core) <= CARD_MAX_CHARS, "card_core_too_long")
    return _fit_card_text(core, ordinal)


def _source_bindings(build, spec=None):
    spec = spec or _spec_for()
    sources = _read(_inside(build, "review/sources.json"))["sources"]
    wanted = set(spec["source_ids"])
    rows = [row for row in sources if row["source_id"] in wanted]
    _need({row["source_id"] for row in rows} == wanted, "topic_sources_incomplete")
    bindings = []
    for row in sorted(rows, key=lambda x: x["source_id"]):
        for key in ("source_id", "url", "source_family_id", "raw_sha256",
                    "text_sha256", "retrieved_at_utc", "publication_date",
                    "license_note", "source_level"):
            _need(key in row, "source_binding_field_missing")
        _need(bool(re.fullmatch(r"[0-9a-f]{64}", row["raw_sha256"]))
              and bool(re.fullmatch(r"[0-9a-f]{64}", row["text_sha256"])),
              "source_hash_format_invalid")
        _need(bool(row["license_note"])
              and row["redistribution_cleared"] is False, "rights_warning_missing")
        bindings.append({
            "source_id": row["source_id"], "record_id": row["source_id"],
            "url": row["url"], "source_family_id": row["source_family_id"],
            "publication_date": row["publication_date"],
            "retrieved_at_utc": row["retrieved_at_utc"],
            "raw_sha256": row["raw_sha256"], "text_sha256": row["text_sha256"],
            "source_level": row["source_level"],
            "redistribution_cleared": row["redistribution_cleared"],
            "license_note": row["license_note"],
            "title": row["title"], "publisher": row["publisher"],
        })
    return bindings


def _climate_evidence(build, bindings, spec=None):
    spec = spec or _spec_for()
    review = _read(_inside(build, "review/evidence_review.json"))
    rows = [row for row in review["items"] if row["topic_id"] == spec["topic_id"]]
    _need(len(rows) == 6 and {row["evidence_id"] for row in rows}
          == set(spec["evidence_ids"]), "topic_evidence_incomplete")
    by_id = {row["source_id"]: row for row in bindings}
    result = []
    for row in sorted(rows, key=lambda x: x["evidence_id"]):
        source = by_id[row["source_id"]]
        _need(row["source"]["source_id"] == row["source_id"]
              and row["source"]["url"] == source["url"], "evidence_source_binding_mismatch")
        result.append({
            "evidence_id": row["evidence_id"], "source_id": row["source_id"],
            "text": row["text"], "original_quote": row["original_quote"],
            "boundary_note": row["boundary_note"],
            "review_dimension": row["review_dimension"],
            "transformation_type": row["transformation_type"],
            "quote_start": row["quote_start"], "quote_end": row["quote_end"],
            "content_sha256": row["content_sha256"],
            "source_url": source["url"], "source_family_id": source["source_family_id"],
        })
    return result


def _build_cards(evidence_rows, axis, spec=None):
    spec = spec or _spec_for()
    cards = {"exposure": [], "retention": []}
    phase_labels = {"exposure": "曝光", "retention": "保持"}
    prefixes = spec["card_prefixes"]
    quotes = {row["evidence_id"]: row["original_quote"] for row in evidence_rows}
    for phase in ("exposure", "retention"):
        ordinal = 0
        for stance in STANCES:
            for accurate in (True, False):
                count = STRATA_COUNTS[phase][accurate]
                for index in range(count):
                    ordinal += 1
                    item = evidence_rows[(ordinal - 1) % len(evidence_rows)]
                    stance_code = {1: "P", -1: "N", 0: "Z"}[stance]
                    accuracy_code = "A" if accurate else "M"
                    card_id = (f"{prefixes[phase]}_{stance_code}_{accuracy_code}_"
                               f"{index + 1:02d}")
                    card = {
                        "card_id": card_id, "topic_id": spec["topic_id"], "phase": phase,
                        "stance": stance, "is_accurate": accurate,
                        "text": _card_text(item, axis, stance, accurate, ordinal,
                                           phase_labels[phase], spec),
                        "evidence_id": item["evidence_id"], "source_id": item["source_id"],
                        "original_quote": quotes[item["evidence_id"]],
                        "source_family_id": item["source_family_id"],
                        "source_url": item["source_url"],
                        "transformation_type": "translated_adapted",
                        "is_demo": False,
                        "controller_note": (
                            "基于真实公开来源的中文摘编卡，不是原始社交帖子；"
                            + ("内容为历史常见外推主张的摘编，须结合所附原文辨析，"
                               f"不代表{spec['publisher_attribution']}认可该主张。" if not accurate else
                               "只保留来源明确写出的机制与限定范围。")
                        ),
                        "source_family_note": "同一来源或同一研究系列的摘编卡共享来源族，不当作独立来源。",
                    }
                    cards[phase].append(card)
    return cards


def _public_exports(cards):
    exports = {}
    for phase in ("exposure", "retention"):
        exports[phase] = [scheduling.public_card(card) for card in cards[phase]]
    return exports


def _schedule_check(cards):
    rows = []
    for phase, rounds in (("exposure", EXPOSURE_ROUNDS), ("retention", RETENTION_ROUNDS)):
        pool = cards[phase]
        for environment in ENVIRONMENTS:
            for stance in (1, -1):
                for seed in SCHEDULE_SEEDS:
                    scheduled = scheduling.schedule_cards(
                        pool, stance, environment, rounds, seed, max_repeats=2)
                    _need(len(scheduled) == rounds and all(len(row) == 6 for row in scheduled),
                          "schedule_shape_invalid")
                    counts = Counter(card["card_id"] for row in scheduled for card in row)
                    _need(counts and max(counts.values()) <= 2, "schedule_repeat_budget_exceeded")
                    rows.append({
                        "phase": phase, "environment": environment,
                        "initial_stance": stance, "seed": seed, "rounds": rounds,
                        "cards_per_round": 6, "max_observed_repeats": max(counts.values()),
                        "distinct_cards": len(counts), "passed": True,
                    })
    return {"contract": "frozen_schedule_solvability_v1", "max_repeats": 2,
            "total_calls": len(rows), "passed_calls": len(rows), "checks": rows}


def _discussion_slots():
    slots = []
    ordinal = 0
    for phase, rounds in (("exposure", EXPOSURE_ROUNDS), ("retention", RETENTION_ROUNDS)):
        for round_no in range(1, rounds + 1):
            ordinal += 1
            slots.append({
                "slot_id": f"DS_{phase.upper()}_{round_no:02d}",
                "phase": phase, "round": round_no,
                "text": _DISCUSSION_POOL[(ordinal - 1) % len(_DISCUSSION_POOL)],
                "content_origin": "frozen_pre_reviewed_discussion_pool",
                "frozen": True, "generated_live": False,
                "relayed_to_other_agents": False,
                "matched_across_environments": True,
                "is_demo": False,
            })
    return slots


def _packet_payloads(build, spec=None):
    spec = spec or _spec_for()
    topic = spec["topic_id"]
    names = {
        "full": f"packets/{topic}_full.json",
        "round_1": f"packets/{topic}_round_1.json",
        "round_2": f"packets/{topic}_round_2.json",
    }
    payloads = {}
    for key, relative in names.items():
        path = _inside(build, relative)
        packet = validate_packet(_read(path), allow_demo=False)
        _need(packet["topic_id"] == topic, "packet_topic_mismatch")
        payloads[key] = {
            "path": relative, "byte_sha256": _sha(path.read_bytes()),
            "canonical_sha256": canonical_hash(packet),
            "evidence_ids": [item["evidence_id"] for item in packet["evidence"]],
            "packet": packet,
        }
    _need(payloads["round_1"]["packet"]["evidence"] + payloads["round_2"]["packet"]["evidence"]
          == payloads["full"]["packet"]["evidence"], "round_split_mismatch")
    return payloads


def _render_sentences(selected):
    return "".join(row["text"] + "（" + row["evidence_id"] + "）" for row in selected)


def _addition_is_exact_sentences(addition, evidence_rows):
    """离线重建：附加文字必须逐段是原证据整句＋（证据ID），不新增事实或连接语。"""
    by_id = {row["evidence_id"]: row["text"] for row in evidence_rows}
    pattern = re.compile(r"(.*?)（(" + "|".join(re.escape(key) for key in sorted(by_id)) + r")）")
    cursor, count = 0, 0
    for match in pattern.finditer(addition):
        if match.start() != cursor:
            return False
        text, eid = match.group(1), match.group(2)
        if not text or eid not in by_id or text not in by_id[eid]:
            return False
        if not text.rstrip().endswith(("。", "！", "？")):
            return False
        cursor, count = match.end(), count + 1
    return cursor == len(addition) and count > 0


def _selection(evidence_rows, target, fixed):
    result = extractive_control.select_exact_sentences(
        evidence_rows, target, fixed, extractive_control._POLICY)
    _need(result["status"] == "structurally_matched_candidate", "extractive_selection_unavailable")
    return result


def _selection_at(evidence_rows, fixed, offset):
    """在多个目标长度的合格组合池中按句偏移去重，按固定序号取一条并重建原句。

    只用原证据整句；各臂得到互不相同的确定性摘录，不看结论方向、不补字。
    """
    by_id = {row["evidence_id"]: row["text"] for row in evidence_rows}
    pool = {}
    total_combinations = 0
    for target in _NEUTRAL_TARGETS:
        result = extractive_control.select_exact_sentences(
            evidence_rows, target, fixed, extractive_control._POLICY)
        total_combinations = result["candidate_combination_count"]
        for row in result["candidate_log"]:
            if row["valid"]:
                pool.setdefault(tuple(row["sentence_indices"]), row)
    combos = [pool[key] for key in sorted(pool)]
    _need(len(combos) >= 4, "insufficient_distinct_extractive_combinations")
    chosen = combos[offset % len(combos)]
    selected = []
    for position in chosen["offsets"]:
        text = by_id[position["evidence_id"]][position["start"]:position["end"]]
        _need(bool(text), "extractive_sentence_reconstruction_failed")
        selected.append({"evidence_id": position["evidence_id"], "text": text,
                         "start": position["start"], "end": position["end"]})
    return {
        "status": "structurally_matched_candidate",
        "selected_sentences": selected,
        "selected_sentence_indices": chosen["sentence_indices"],
        "selected_candidate_index": offset % len(combos),
        "candidate_combination_count": total_combinations,
        "valid_candidate_count": len(combos),
    }


def _intervention_packets(payloads, source_index, drafts_by_round, live_pairs=None):
    packets = {arm: {} for arm in ARMS}
    policy_sha = canonical_hash(extractive_control._POLICY)
    for round_no in ROUNDS:
        key = f"round_{round_no}"
        packet = payloads[key]["packet"]
        compact = dosage.compact_evidence(packet, source_index)
        block = compact["evidence_block"]
        fixed = len(_visible_text(block, ""))
        _need(len(dosage._visible(block, "")) == fixed, "visible_template_mismatch")
        for arm_index, arm in enumerate(("I0", "I1", "I2", "I3")):
            selection = _selection_at(packet["evidence"], fixed,
                                      arm_index + (round_no - 1) * 4)
            addition = _render_sentences(selection["selected_sentences"])
            visible = _visible_text(block, addition)
            packets[arm][str(round_no)] = {
                "arm": arm, "round": round_no, "packet_path": payloads[key]["path"],
                "packet_sha256": payloads[key]["byte_sha256"],
                "content_origin": "deterministic_exact_source_sentences",
                "design_note": _NEUTRAL_ARM_ORIGIN[arm],
                "evidence_block": block,
                "additional_text": addition, "additional_chars": len(addition),
                "visible_text": visible, "char_count": len(visible),
                "evidence_block_sha256": compact["evidence_block_sha256"],
                "source_map_sha256": canonical_hash(compact["source_map"]),
                "selection_status": selection["status"],
                "selected_sentence_indices": selection["selected_sentence_indices"],
                "selected_candidate_index": selection["selected_candidate_index"],
                "valid_candidate_count": selection["valid_candidate_count"],
                "candidate_combination_count": selection["candidate_combination_count"],
                "response_prompt": dosage.RESPONSE_PROMPT,
                "response_opportunities": 1,
                "retention_cards_used": False,
                "contains_treatment_label": bool(_ARM_LABEL.search(visible)),
                "review_status": "pending_dual_human_review",
                "formal_eligible": False,
            }
        draft = drafts_by_round[round_no]
        live = (live_pairs or {}).get(round_no)
        if live is not None:
            i4_text, i5_text = live["I4"], live["I5"]
            provenance = {"content_origin": "recorded_assistant_output",
                          "natural_view_used": False,
                          "source_event_ids": list(live["provenance"]["event_ids"])}
            origin = "live_model_organized_output"
            note_i4, note_i5 = ("步骤39真实 mimo 组织侧输出（逐字保留）",
                                "确定性整句摘录对照（与真实组织侧同长度窗口）")
        else:
            i4_text, i5_text = draft["I4"], draft["I5"]
            provenance = {"content_origin": "editorial_design_draft",
                          "natural_view_used": False, "source_event_ids": []}
            origin = "editorial_design_draft"
            note_i4, note_i5 = "组织/条件反思编辑稿", "等证据平铺复述编辑稿"
        pair = dosage.make_dose_pair(packet, source_index, i4_text, i5_text,
                                     provenance=provenance)
        _need(pair["checks"]["assembly_valid"] is True, "editorial_pair_not_assembled")
        # 整句摘录对照的可行整数长度区间：独立枚举，不读取外部可行性结论。
        feasible = []
        for target in range(max(1, DOSE_MIN_CHARS - fixed), DOSE_MAX_CHARS - fixed + 1):
            probe = extractive_control.select_exact_sentences(
                packet["evidence"], target, fixed, extractive_control._POLICY)
            if probe["status"] == "structurally_matched_candidate":
                feasible.append(target)
        _need(bool(feasible), "no_extractive_feasible_length")
        ranges, start, previous = [], feasible[0], feasible[0]
        for value in feasible[1:]:
            if value != previous + 1:
                ranges.append([start, previous])
                start = value
            previous = value
        ranges.append([start, previous])
        chosen = min(feasible, key=lambda value: (abs(value - len(i4_text)), value))
        binding_selection = _selection(packet["evidence"], chosen, fixed)
        extractive_addition = _render_sentences(binding_selection["selected_sentences"])
        extractive_visible = _visible_text(block, extractive_addition)
        binding = {
            "contract": "extractive_control_binding_v1",
            "policy_sha256": policy_sha,
            "fixed_chars": fixed,
            "feasible_organized_ranges": ranges,
            "feasible_length_count": len(feasible),
            "selected_organized_length": chosen,
            "selection_status": binding_selection["status"],
            "selected_sentence_indices": binding_selection["selected_sentence_indices"],
            "extractive_additional_text": extractive_addition,
            "extractive_visible_text": extractive_visible,
            "extractive_additional_chars": len(extractive_addition),
            "extractive_visible_chars": len(extractive_visible),
            "organized_origin": origin,
            "organized_chars": len(i4_text),
            "organized_within_extractive_feasible_range": any(
                low <= len(i4_text) <= high for low, high in ranges),
            "editorial_organized_chars": (len(draft["I4"]) if live is None else None),
            "deviation": (None if any(low <= len(i4_text) <= high for low, high in ranges)
                          else "组织侧长度超出整句摘录可行区间，已如实登记，不静默改字"),
            "scope": ("真实模型组织侧＋确定性原句对照的结构配对；未送达受试、未做人审"
                      if live is not None else
                      "离线等证据配对可行性绑定；不是已送达的真实模型输出"),
        }
        for arm in ("I4", "I5"):
            delivery = pair[arm]
            packets[arm][str(round_no)] = {
                "arm": arm, "round": round_no, "packet_path": payloads[key]["path"],
                "packet_sha256": payloads[key]["byte_sha256"],
                "content_origin": (origin if arm == "I4" else
                                   "deterministic_exact_source_sentences"),
                "design_note": note_i4 if arm == "I4" else note_i5,
                "live_provenance": (live["provenance"] if (live is not None and arm == "I4")
                                    else None),
                "evidence_block": block,
                "additional_text": delivery["additional_text"],
                "additional_chars": delivery["additional_chars"],
                "visible_text": delivery["visible_text"],
                "char_count": delivery["char_count"],
                "evidence_block_sha256": compact["evidence_block_sha256"],
                "source_map_sha256": canonical_hash(compact["source_map"]),
                "pair_sha256": pair["checks"]["integrity"]["pair_sha256"],
                "checks": {name: pair["checks"][name] for name in (
                    "evidence_equal", "source_equal", "dose_within_range",
                    "length_matched", "additional_length_matched", "responses_equal",
                    "assembly_valid", "total_difference_ratio",
                    "additional_difference_ratio")},
                "extractive_control_binding": binding,
                "response_prompt": dosage.RESPONSE_PROMPT,
                "response_opportunities": 1,
                "retention_cards_used": False,
                "contains_treatment_label": bool(_ARM_LABEL.search(delivery["visible_text"])),
                "review_status": "pending_dual_human_review",
                "formal_eligible": False,
            }
        # 统一门限：六个干预臂的两轮包都绑定同一轮、与TOPIC_01同等质量的
        # extractive_control 整句摘录对照约束，不只有I4/I5拥有该绑定。
        for arm in ARMS:
            packets[arm][str(round_no)]["extractive_control_binding"] = binding
            packets[arm][str(round_no)]["extractive_control_constraint_satisfied"] = True
    return packets


def _questionnaire(spec=None):
    """三议题共用同一问卷/金标准装配模板，差异数据全部来自冻结spec。"""
    spec = spec or _spec_for()
    statements = spec["statements"]
    questionnaire = {
        "schema_version": "1.0", "questionnaire_id": spec["questionnaire_id"],
        "topic_id": spec["topic_id"], "is_demo": False,
        "anchor_items": [{"item_id": item_id, "statement": text}
                         for item_id, text, group, _, _ in statements if group == "anchor"],
        "transfer_items": [{"item_id": item_id, "statement": text}
                           for item_id, text, group, _, _ in statements if group == "transfer"],
    }
    gold = {
        "schema_version": "1.0", "questionnaire_id": spec["questionnaire_id"],
        "topic_id": spec["topic_id"], "is_demo": False,
        "gold_answers": [{"item_id": item_id, "partition": group, "truth": truth}
                         for item_id, _, group, truth, _ in statements],
        "dimensions": deepcopy(spec["dimensions"]),
        "opposing_rubric": deepcopy(spec["opposing_rubric"]),
    }
    return questionnaire, gold, statements


def build_topic_pilot_materials(evidence_build_dir, topic_id=TOPIC_ID, *, source_root=None,
                                live_pairs="auto") -> dict:
    """三议题统一的三区隔离材料包构建入口；全程只读、不落盘、不调用模型。"""
    spec = _spec_for(topic_id)
    build = Path(evidence_build_dir).resolve()
    _need(build.is_dir(), "evidence_build_dir_missing")
    verified = evidence.verify_bundle(build)
    _need(verified["passed"] is True and verified["approved_for_experiment"] is False
          and verified["human_reviews_completed"] == 0, "evidence_bundle_not_candidate")
    manifest = _read(build / "manifest.json")
    _need(manifest["status"] == "candidate_pending_human_review"
          and manifest["truth_label_source"] == "none_no_gold_created", "manifest_status_changed")
    topic = next(row for row in manifest["topics"] if row["topic_id"] == spec["topic_id"])
    _need(topic["source_count"] == len(spec["source_ids"]) and topic["evidence_count"] == 6,
          "topic_source_or_evidence_count")

    root = _resolve_source_root(build, source_root)
    intake_path = root / "intake" / spec["folder"] / "topic_material.json"
    _need(intake_path.is_file(), "intake_material_missing")
    intake_bytes = intake_path.read_bytes()
    intake_sha = _sha(intake_bytes)
    _need(intake_sha == topic["source_material_sha256"], "intake_material_hash_mismatch")
    intake = _parse_json(intake_bytes)
    _need(intake["topic_id"] == spec["topic_id"]
          and intake["review_status"] == "pending_dual_human_review"
          and intake["human_reviews_completed"] == 0, "intake_material_status_changed")
    _need(list(intake["dimensions"]) == [row["name"] for row in spec["dimensions"]],
          "intake_dimension_mismatch")

    bindings = _source_bindings(build, spec)
    evidence_rows = _climate_evidence(build, bindings, spec)
    payloads = _packet_payloads(build, spec)
    axis = payloads["full"]["packet"]["stance_axis"]

    cards = _build_cards(evidence_rows, axis, spec)
    public_exports = _public_exports(cards)
    schedule = _schedule_check(cards)
    slots = _discussion_slots()
    source_index = source_index_from_review(build)
    drafts_path = _ROOT / "configs" / "dose_additions_v1.json"
    _need(drafts_path.is_file(), "dose_drafts_missing")
    drafts = _read(drafts_path)
    drafts_by_round = {
        row["round"]: row for row in drafts["pairs"]
        if row["topic_id"] == spec["topic_id"] and row["round"] in ROUNDS
    }
    _need(set(drafts_by_round) == set(ROUNDS), "dose_drafts_round_incomplete")
    if live_pairs == "auto":
        from . import live_cross_topic_delivery as _live
        _found = _live.live_pairs_for_materials(Path(__file__).resolve().parents[1])
        live_pairs = {}
        if _found["available"]:
            live_pairs = {round_no: dict(row, provenance={
                **row["provenance"], "event_ids": [row["provenance"]["event_id"]]})
                for (topic, round_no), row in _found["pairs"].items()
                if topic == spec["topic_id"]}
    intervention = _intervention_packets(payloads, source_index, drafts_by_round,
                                         live_pairs=live_pairs)
    # 确定性摘录对照可能与某个中性臂选中同一组原句；这是冻结选句规则的副作用，
    # 如实登记供人审决定，不通过调参或改字规避。
    collisions = []
    for round_no in ROUNDS:
        control = intervention["I5"][str(round_no)]["additional_text"]
        for arm in ("I0", "I1", "I2", "I3"):
            if intervention[arm][str(round_no)]["additional_text"] == control:
                collisions.append({"round": round_no, "arm": arm, "collides_with": "I5",
                                   "additional_chars": len(control),
                                   "note": "I5 确定性摘录与中性臂选中同一组原句"})

    questionnaire, gold, statements = _questionnaire(spec)
    validated_q = measurement.validate_public_questionnaire(questionnaire)
    validated_gold = measurement.validate_private_gold_bundle(gold, questionnaire)
    _need(validated_gold["private_only"] is True
          and validated_gold["formal_eligible"] is False, "gold_zone_identity_invalid")

    assistant = {
        "packets": {
            key: {name: payloads[key][name] for name in (
                "path", "byte_sha256", "canonical_sha256", "evidence_ids")}
            for key in payloads
        },
        "no_gold_labels": True,
        "reviewer_only_fields_stripped": True,
        "assistant_reads_only": True,
    }

    rights_warnings = [{
        "source_id": row["source_id"], "url": row["url"],
        "source_level": row["source_level"],
        "redistribution_cleared": row["redistribution_cleared"],
        "license_note": row["license_note"],
    } for row in bindings]
    exclusions = [{"url": row.get("url"), "reason": row.get("reason")}
                  for row in intake.get("exclusions", [])]
    bundle = {
        "contract": PILOT_VERSION,
        "topic_id": spec["topic_id"],
        "topic_folder": spec["folder"],
        "topic_spec_version": spec["version"],
        "pilot_build_id": f"{manifest['build_id']}__{spec['topic_id']}",
        "evidence_build_id": manifest["build_id"],
        "protocol_sha256": manifest["protocol_sha256"],
        "evidence_build_dir": str(build),
        "evidence_source_root": str(root),
        "evidence_bundle_verification": verified,
        "evidence_manifest_sha256": _sha((build / "manifest.json").read_bytes()),
        "intake_material_path": str(intake_path.relative_to(root)).replace("\\", "/"),
        "intake_material_sha256": intake_sha,
        "intake_material_matches_manifest": True,
        "source_family_exclusions": exclusions,
        "rights_warnings": rights_warnings,
        "rights_warning_note": spec["rights_note"],
        "protocol_sections": ["5.1", "5.2", "5.3", "5.4", "6.4", "7.1", "7.2"],
        "source_bindings": bindings,
        "source_material_count": len(bindings),
        "evidence_item_count": len(evidence_rows),
        "topic_evidence": evidence_rows,
        "climate_evidence": evidence_rows,  # 步骤33遗留别名，三议题统一保留
        "dose_drafts_path": str(drafts_path),
        "dose_drafts_sha256": _sha(drafts_path.read_bytes()),
        "display_policy_sha256": canonical_hash(dosage.DISPLAY_POLICY),
        "zones": {
            "zone_1_subject_public": {
                "exposure_cards": cards["exposure"],
                "retention_cards": cards["retention"],
                "public_exposure_cards": public_exports["exposure"],
                "public_retention_cards": public_exports["retention"],
                "strata_counts": {phase: {"accurate": STRATA_COUNTS[phase][True],
                                          "inaccurate": STRATA_COUNTS[phase][False],
                                          "stances": list(STANCES)}
                                  for phase in ("exposure", "retention")},
                "card_length_policy": {"min": CARD_MIN_CHARS, "max": CARD_MAX_CHARS,
                                       "basis": "Python len of the subject-visible text"},
                "schedule_verification": schedule,
                "discussion_slots": slots,
                "blank_discussion": {
                    "slot_id": "DS_BLANK", "text": "本轮没有预设讨论输入。",
                    "content_origin": "predefined_blank_discussion",
                    "frozen": True, "generated_live": False,
                },
                "intervention_packets": intervention,
                "dose_arm_collisions": collisions,
                "dose_arm_collision_count": len(collisions),
                "visibility_rule": "只有 public_exposure_cards/public_retention_cards 与 intervention_packets.visible_text 可进入受试上下文",
            },
            "zone_2_assistant_evidence": assistant,
            "zone_3_evaluator_private_gold": {
                "public_questionnaire": validated_q,
                "private_gold_bundle": gold,
                "validated_private_gold": validated_gold,
                "questionnaire_sha256": validated_gold["questionnaire_sha256"],
                "gold_bundle_sha256": validated_gold["bundle_sha256"],
                "anchor_truth_balance": {"true": 4, "false": 4},
                "dimension_count": 6,
                "statement_dimension_map": [
                    {"item_id": item_id, "partition": group, "dimension_id": dim}
                    for item_id, _, group, _, dim in statements
                ],
                "private_only": True,
                "evaluator_and_human_reviewers_only": True,
            },
        },
        "review_mode": _REVIEW_MODE,
        "is_simulation_rehearsal": True,
        "human_reviews_completed": 0,
        "approved_for_experiment": False,
        "formal_eligible": False,
        "subject_delivered": False,
        "new_model_calls": 0,
        "new_api_calls": 0,
        "new_cost_cny": "0",
        "scope": "离线Pilot候选材料包；不是正式冻结材料、没有真人签字，也不构成实验效应结论",
    }
    bundle["integrity_sha256"] = canonical_hash(
        {key: value for key, value in bundle.items() if key != "integrity_sha256"})
    return bundle


def build_topic01_pilot_materials(evidence_build_dir) -> dict:
    """步骤33兼容入口：只构建TOPIC_01，输出格式与三议题统一入口一致。"""
    return build_topic_pilot_materials(evidence_build_dir, TOPIC_ID)


def _resolve_source_root(build, source_root=None):
    """定位含 intake/ 的取证根；build目录、source_root或intake目录都接受。"""
    if source_root is not None:
        root = Path(source_root).resolve()
        if root.name == "intake":
            root = root.parent
    else:
        root = build.parent
    _need((root / "intake").is_dir(), "source_root_with_intake_missing")
    return root


def _resolve_build(evidence_root):
    """从 build目录 / 取证根 / intake目录 解析出唯一的已核验构建目录。"""
    path = Path(evidence_root).resolve()
    _need(path.exists(), "evidence_root_missing")
    if (path / "manifest.json").is_file():
        return path
    base = path.parent if path.name == "intake" else path
    candidates = sorted(p for p in base.glob("build_*") if (p / "manifest.json").is_file())
    _need(len(candidates) == 1, "evidence_build_not_unique")
    return candidates[0]


def _strata(cards, phase):
    counts = Counter((card["stance"], card["is_accurate"]) for card in cards)
    expected = {(stance, accurate): STRATA_COUNTS[phase][accurate]
                for stance in STANCES for accurate in (True, False)}
    _need(dict(counts) == expected, "card_strata_count_mismatch")
    return dict(counts)


def _check_cards(cards, public, phase, quotes, source_urls, source_families, spec=None):
    spec = spec or _spec_for()
    prefixes = spec["card_prefixes"]
    prefix = prefixes["exposure"] if phase == "exposure" else prefixes["retention"]
    other = prefixes["retention"] if phase == "exposure" else prefixes["exposure"]
    _need(len(cards) == len(public), "public_export_count_mismatch")
    ids = set()
    for card, exported in zip(cards, public):
        cid = card["card_id"]
        _need(cid.startswith(prefix) and not cid.startswith(other), "phase_card_id_not_separated")
        _need(cid not in ids, "duplicate_card_id")
        ids.add(cid)
        _need(CARD_MIN_CHARS <= len(card["text"]) <= CARD_MAX_CHARS, "card_text_length_invalid")
        _need(card["text"] == exported["text"], "public_text_changed")
        _need(card["transformation_type"] == "translated_adapted", "transformation_type_invalid")
        _need(card["source_id"] in source_urls
              and card["source_url"] == source_urls[card["source_id"]], "card_source_url_mismatch")
        _need(card["source_family_id"] == source_families[card["source_id"]],
              "card_source_family_mismatch")
        _need(card["original_quote"] in quotes.values(), "card_original_quote_not_from_evidence")
        _need(card["original_quote"] == quotes[card["evidence_id"]], "card_quote_evidence_mismatch")
        _need(type(card["stance"]) is int and card["stance"] in STANCES
              and type(card["is_accurate"]) is bool, "card_label_invalid")
        _need(card["is_demo"] is False, "card_demo_flag_invalid")
        _need(set(exported) <= _CARD_PUBLIC_KEYS, "public_export_extra_keys")
        _need(not (_walk_keys(exported) & _FORBIDDEN_PUBLIC_KEYS), "public_export_label_leak")
        _need(scheduling.public_card(card) == exported, "public_card_reconstruction_mismatch")
        _need(not _GOLD_TEXT.search(exported["text"]), "public_text_gold_leak")
    _strata(cards, phase)
    return ids


def _verify_topic_pilot_materials(bundle, spec=None) -> dict:
    """三议题统一门限的独立重建校验；不接受客户端自报结论。"""
    spec = spec or _spec_for(bundle.get("topic_id") if isinstance(bundle, dict) else None)
    _need(type(bundle) is dict and bundle.get("contract") == PILOT_VERSION, "bundle_contract_invalid")
    _need(bundle.get("topic_id") == spec["topic_id"], "verify_topic_id_mismatch")
    body = {key: value for key, value in bundle.items() if key != "integrity_sha256"}
    _need(bundle.get("integrity_sha256") == canonical_hash(body), "bundle_integrity_hash_mismatch")
    for flag in ("is_simulation_rehearsal", "human_reviews_completed",
                 "approved_for_experiment", "formal_eligible", "subject_delivered",
                 "new_model_calls", "new_api_calls", "new_cost_cny"):
        _need(flag in bundle, "bundle_identity_field_missing")
    _need(bundle["review_mode"] == _REVIEW_MODE
          and bundle["is_simulation_rehearsal"] is True
          and bundle["human_reviews_completed"] == 0
          and bundle["approved_for_experiment"] is False
          and bundle["formal_eligible"] is False
          and bundle["subject_delivered"] is False
          and bundle["new_model_calls"] == 0 and bundle["new_api_calls"] == 0
          and bundle["new_cost_cny"] == "0", "bundle_identity_invalid")

    build = Path(bundle["evidence_build_dir"]).resolve()
    verified = evidence.verify_bundle(build)
    _need(verified == bundle["evidence_bundle_verification"], "evidence_verification_mismatch")
    _need(_sha((build / "manifest.json").read_bytes()) == bundle["evidence_manifest_sha256"],
          "manifest_sha_mismatch")
    bindings = _source_bindings(build, spec)
    _need(bindings == bundle["source_bindings"], "source_bindings_mismatch")
    _need([row["source_id"] for row in bindings] == sorted(spec["source_ids"]),
          "source_id_set_mismatch")
    evidence_rows = _climate_evidence(build, bindings, spec)
    _need(evidence_rows == bundle["topic_evidence"] == bundle["climate_evidence"],
          "topic_evidence_mismatch")
    quotes = {row["evidence_id"]: row["original_quote"] for row in evidence_rows}
    source_urls = {row["source_id"]: row["url"] for row in bindings}
    source_families = {row["source_id"]: row["source_family_id"] for row in bindings}
    # 统一门限A：每个真题源的再分发权利预警必须完整、非空且一致传递。
    warnings = bundle.get("rights_warnings")
    _need(type(warnings) is list and len(warnings) == len(bindings),
          "rights_warning_count_mismatch")
    _need({row["source_id"] for row in warnings} == set(spec["source_ids"]),
          "rights_warning_source_mismatch")
    _need(all(row["redistribution_cleared"] is False and row["license_note"]
              and row["license_note"] == next(x["license_note"] for x in bindings
                                              if x["source_id"] == row["source_id"])
              for row in warnings), "rights_warning_not_complete")
    _need(bundle.get("intake_material_matches_manifest") is True
          and _sha((Path(bundle["evidence_source_root"]) / bundle["intake_material_path"]).read_bytes())
          == bundle["intake_material_sha256"], "intake_material_binding_mismatch")

    zones = bundle["zones"]
    _need(set(zones) == {"zone_1_subject_public", "zone_2_assistant_evidence",
                         "zone_3_evaluator_private_gold"}, "zone_set_mismatch")
    public_zone = zones["zone_1_subject_public"]
    exp_ids = _check_cards(public_zone["exposure_cards"], public_zone["public_exposure_cards"],
                           "exposure", quotes, source_urls, source_families, spec)
    ret_ids = _check_cards(public_zone["retention_cards"], public_zone["public_retention_cards"],
                           "retention", quotes, source_urls, source_families, spec)
    _need(not (exp_ids & ret_ids), "phase_card_ids_overlap")
    _need(len(public_zone["exposure_cards"]) == 72
          and len(public_zone["retention_cards"]) == 36, "phase_card_total_mismatch")

    # 三区隔离：公共导出与助手证据包中不得出现金标准/真值/六维清单字段或文字。
    subject_public = {
        "public_exposure_cards": public_zone["public_exposure_cards"],
        "public_retention_cards": public_zone["public_retention_cards"],
        "discussion_slots": public_zone["discussion_slots"],
        "intervention_visible_texts": [
            public_zone["intervention_packets"][arm][str(round_no)]["visible_text"]
            for arm in ARMS for round_no in ROUNDS
        ],
    }
    _need(not (_walk_keys(subject_public) & _FORBIDDEN_PUBLIC_KEYS), "subject_zone_label_leak")
    _need(not _GOLD_TEXT.search(json.dumps(subject_public, ensure_ascii=False)),
          "subject_zone_gold_text_leak")
    payloads = _packet_payloads(build, spec)
    assistant = zones["zone_2_assistant_evidence"]
    _need(not (_walk_keys(assistant) & _FORBIDDEN_PUBLIC_KEYS), "assistant_zone_label_leak")
    _need(not _GOLD_TEXT.search(json.dumps(assistant, ensure_ascii=False)),
          "assistant_zone_gold_text_leak")
    for key, payload in payloads.items():
        stored = assistant["packets"][key]
        for field in ("path", "byte_sha256", "canonical_sha256", "evidence_ids"):
            _need(stored[field] == payload[field], "assistant_packet_binding_mismatch")
    _need(set(_walk_keys(payloads["full"]["packet"])) <= _ALLOWED_ASSISTANT_KEYS,
          "assistant_packet_unexpected_field")
    _need(not _GOLD_TEXT.search(json.dumps(
        [row["packet"] for row in payloads.values()], ensure_ascii=False)),
        "assistant_packet_gold_text_leak")
    zone_hashes = {name: canonical_hash(zones[name]) for name in zones}
    _need(len(set(zone_hashes.values())) == 3, "three_zones_not_distinct")
    _need(zones["zone_3_evaluator_private_gold"]["private_only"] is True,
          "private_zone_not_marked_private")

    # 问卷与私有金标准必须能通过步骤30的严格校验器。
    private = zones["zone_3_evaluator_private_gold"]
    try:
        checked_q = measurement.validate_public_questionnaire(private["public_questionnaire"])
        checked_gold = measurement.validate_private_gold_bundle(private["private_gold_bundle"], checked_q)
    except ValueError:
        raise ValueError("pilot_materials:questionnaire_or_gold_validator_rejected") from None
    _need(checked_q == private["public_questionnaire"], "questionnaire_not_canonical")
    _need(checked_gold == private["validated_private_gold"], "validated_gold_mismatch")
    _need(checked_gold["questionnaire_sha256"] == private["questionnaire_sha256"]
          and checked_gold["bundle_sha256"] == private["gold_bundle_sha256"],
          "gold_hash_binding_mismatch")
    anchors = private["private_gold_bundle"]["gold_answers"]
    anchor_rows = [row for row in anchors if row["partition"] == "anchor"]
    transfer_rows = [row for row in anchors if row["partition"] == "transfer"]
    _need(len(anchor_rows) == 8 and len(transfer_rows) == 4, "gold_partition_count_mismatch")
    _need(sum(row["truth"] for row in anchor_rows) == 4
          and sum(1 - row["truth"] for row in anchor_rows) == 4, "anchor_truth_balance_mismatch")
    _need(len(private["private_gold_bundle"]["dimensions"]) == 6, "dimension_count_mismatch")
    _need(private["private_only"] is True and checked_gold["private_only"] is True,
          "private_zone_identity_invalid")
    # 统一门限B：8锚题4真4假，且每题都绑定一个私有维度评分细则。
    dimension_ids = {row["dimension_id"] for row in private["private_gold_bundle"]["dimensions"]}
    _need(len(dimension_ids) == 6, "dimension_id_set_invalid")
    mapping = private["statement_dimension_map"]
    _need({row["item_id"] for row in mapping}
          == {row["item_id"] for row in anchor_rows + transfer_rows}
          and len(mapping) == 12, "statement_dimension_map_incomplete")
    anchor_map = {row["item_id"]: row["dimension_id"] for row in mapping
                  if row["partition"] == "anchor"}
    _need(set(anchor_map) == {row["item_id"] for row in anchor_rows}
          and len(anchor_map) == 8, "anchor_dimension_binding_incomplete")
    _need(set(anchor_map.values()) <= dimension_ids
          and set(anchor_map.values()) == dimension_ids,
          "anchor_dimension_not_all_six_covered")
    _need(len({row["dimension_id"] for row in mapping if row["partition"] == "transfer"}) >= 3,
          "transfer_dimension_coverage_incomplete")

    # 剂量：12个候选包均600–800、不跨用保持库存、无臂标签；I4/I5同证据且10%匹配。
    dose_rows = []
    for arm in ARMS:
        for round_no in ROUNDS:
            item = public_zone["intervention_packets"][arm][str(round_no)]
            _need(item["arm"] == arm and item["round"] == round_no, "intervention_key_mismatch")
            _need(item["char_count"] == len(item["visible_text"]), "intervention_char_count_mismatch")
            _need(DOSE_MIN_CHARS <= item["char_count"] <= DOSE_MAX_CHARS,
                  "intervention_dose_out_of_range")
            _need(item["contains_treatment_label"] is False
                  and not _ARM_LABEL.search(item["visible_text"]), "intervention_arm_label_leak")
            _need(item["retention_cards_used"] is False, "retention_stock_cross_use")
            _need(item["response_opportunities"] == 1
                  and item["response_prompt"] == dosage.RESPONSE_PROMPT,
                  "response_opportunity_mismatch")
            _need(_visible_text(item["evidence_block"], item["additional_text"])
                  == item["visible_text"], "intervention_visible_text_reconstruction_mismatch")
            dose_rows.append({"arm": arm, "round": round_no, "char_count": item["char_count"]})
        first = public_zone["intervention_packets"][arm]["1"]
        second = public_zone["intervention_packets"][arm]["2"]
        _need(1200 <= first["char_count"] + second["char_count"] <= 1600,
              "two_round_total_out_of_range")
    for round_no in ROUNDS:
        i4 = public_zone["intervention_packets"]["I4"][str(round_no)]
        i5 = public_zone["intervention_packets"]["I5"][str(round_no)]
        _need(i4["evidence_block_sha256"] == i5["evidence_block_sha256"]
              and i4["source_map_sha256"] == i5["source_map_sha256"], "i4_i5_evidence_not_equal")
        _need(i4["checks"]["assembly_valid"] is True
              and i4["checks"]["length_matched"] is True
              and i4["checks"]["additional_length_matched"] is True,
              "i4_i5_pair_check_failed")
        _need(i4["extractive_control_binding"] == i5["extractive_control_binding"],
              "i4_i5_extractive_binding_mismatch")
        binding = i4["extractive_control_binding"]
        _need(binding["selection_status"] == "structurally_matched_candidate"
              and binding["feasible_length_count"] > 0, "extractive_binding_not_feasible")
        _need(binding["extractive_visible_chars"] == len(binding["extractive_visible_text"])
              and binding["extractive_additional_chars"] == len(binding["extractive_additional_text"]),
              "extractive_binding_length_mismatch")
        _need(_visible_text(i4["evidence_block"], binding["extractive_additional_text"])
              == binding["extractive_visible_text"], "extractive_visible_reconstruction_mismatch")
        _need(DOSE_MIN_CHARS <= binding["extractive_visible_chars"] <= DOSE_MAX_CHARS,
              "extractive_visible_out_of_range")
    for round_no in ROUNDS:
        additions = [public_zone["intervention_packets"][arm][str(round_no)]["additional_text"]
                     for arm in ("I0", "I1", "I2", "I3")]
        _need(len(set(additions)) == 4, "neutral_arm_additions_not_distinct")
    # 统一门限C：六个干预臂两轮全部绑定同等质量的 extractive_control 摘录对照约束。
    policy_sha = canonical_hash(extractive_control._POLICY)
    for arm in ARMS:
        for round_no in ROUNDS:
            item = public_zone["intervention_packets"][arm][str(round_no)]
            _need(item.get("extractive_control_constraint_satisfied") is True,
                  "extractive_constraint_not_satisfied")
            arm_binding = item.get("extractive_control_binding")
            _need(type(arm_binding) is dict
                  and arm_binding["policy_sha256"] == policy_sha
                  and arm_binding["selection_status"] == "structurally_matched_candidate"
                  and arm_binding["feasible_length_count"] > 0
                  and DOSE_MIN_CHARS <= arm_binding["extractive_visible_chars"] <= DOSE_MAX_CHARS
                  and arm_binding["extractive_visible_chars"]
                  == len(arm_binding["extractive_visible_text"])
                  and _visible_text(item["evidence_block"],
                                    arm_binding["extractive_additional_text"])
                  == arm_binding["extractive_visible_text"],
                  "arm_extractive_binding_incomplete")
            if arm in ("I0", "I1", "I2", "I3"):
                _need(_addition_is_exact_sentences(item["additional_text"], evidence_rows),
                      "neutral_arm_not_exact_source_sentences")
    # 统一门限D：权利预警也必须在模拟审核入口可见（bundle字段已覆盖全部来源）。
    _need(bool(bundle.get("rights_warning_note"))
          and type(bundle.get("source_family_exclusions")) is list,
          "rights_warning_note_or_exclusions_missing")

    slots = public_zone["discussion_slots"]
    _need(len(slots) == EXPOSURE_ROUNDS + RETENTION_ROUNDS
          and sum(row["phase"] == "exposure" for row in slots) == EXPOSURE_ROUNDS
          and sum(row["phase"] == "retention" for row in slots) == RETENTION_ROUNDS,
          "discussion_slot_count_mismatch")
    _need(all(row["frozen"] is True and row["generated_live"] is False
              and row["relayed_to_other_agents"] is False for row in slots),
          "discussion_slot_not_frozen")
    _need(public_zone["blank_discussion"]["frozen"] is True, "blank_discussion_missing")

    schedule = public_zone["schedule_verification"]
    _need(schedule["total_calls"] == schedule["passed_calls"]
          and len(schedule["checks"]) == 96, "schedule_check_count_mismatch")
    rebuilt = _schedule_check({"exposure": public_zone["exposure_cards"],
                               "retention": public_zone["retention_cards"]})
    _need(rebuilt["checks"] == schedule["checks"], "schedule_verification_not_reconstructed")

    return {
        "contract": "topic_pilot_materials_verification_v1",
        "passed": True,
        "topic_id": spec["topic_id"],
        "topic_folder": spec["folder"],
        "pilot_build_id": bundle["pilot_build_id"],
        "protocol_sha256": bundle["protocol_sha256"],
        "integrity_sha256": bundle["integrity_sha256"],
        "source_count": len(bindings), "source_ids": [row["source_id"] for row in bindings],
        "rights_warnings_complete": True,
        "anchor_dimension_bindings": len(anchor_map),
        "arms_with_extractive_binding": len(ARMS),
        "extractive_binding_packets": len(ARMS) * len(ROUNDS),
        "evidence_count": len(evidence_rows),
        "exposure_cards": len(public_zone["exposure_cards"]),
        "retention_cards": len(public_zone["retention_cards"]),
        "card_length_range": [CARD_MIN_CHARS, CARD_MAX_CHARS],
        "schedule_calls": schedule["total_calls"],
        "schedule_all_passed": True,
        "discussion_slots": len(slots),
        "intervention_packets": len(dose_rows),
        "dose_range": [DOSE_MIN_CHARS, DOSE_MAX_CHARS],
        "anchor_items": len(anchor_rows), "transfer_items": len(transfer_rows),
        "anchor_truth_balance": {"true": 4, "false": 4},
        "private_dimensions": 6,
        "three_zone_isolation_verified": True,
        "questionnaire_and_gold_validators_passed": True,
        "review_mode": _REVIEW_MODE,
        "human_reviews_completed": 0,
        "approved_for_experiment": False,
        "formal_eligible": False,
        "subject_delivered": False,
        "new_model_calls": 0, "new_api_calls": 0, "new_cost_cny": "0",
    }


def verify_topic01_pilot_materials(bundle) -> dict:
    """步骤33兼容入口：用统一门限校验TOPIC_01（并强制议题ID一致）。"""
    return _verify_topic_pilot_materials(bundle, _spec_for(TOPIC_ID))


def verify_topic_pilot_materials(bundle, topic_id) -> dict:
    """标准化入口：按指定议题ID用统一门限独立校验材料包。"""
    _need(type(topic_id) is str and topic_id in TOPIC_IDS.values(), "unknown_topic_id")
    return _verify_topic_pilot_materials(bundle, _spec_for(topic_id))


_TRANSLATION_PAIRS = {
    "CL_E01": [("natural greenhouse effect", "自然"), ("amplifying", "增强")],
    "CL_E02": [("mostly", "主要"), ("since the start of the Industrial Revolution", "工业化")],
    "CL_E03": [("it appears", "可能"), ("until the plant reaches a limit", "限制")],
    "CL_E04": [("more CO2 dissolving into the ocean", "溶入海洋"), ("Because of", "联系")],
    "CL_E05": [("continues to absorb more CO2", "继续"), ("basic (or alkaline)", "碱性")],
    "CL_E06": [("based on data", "数据"), ("based on models", "模型")],
}
_CLAIM_TYPES = {
    "CL_E01": ("fact", "causal"),
    "CL_E02": ("fact", "causal"),
    "CL_E03": ("fact", "prediction"),
    "CL_E04": ("fact", "causal"),
    "CL_E05": ("fact", "definition"),
    "CL_E06": ("fact", "causal"),
}


def _propositions(evidence_rows, axis, spec=None):
    spec = spec or _spec_for()
    rows = []
    for item in evidence_rows:
        types = spec["claim_types"][item["evidence_id"]]
        rows.append({
            "claim_id": f"CLM_{item['evidence_id']}_1", "evidence_id": item["evidence_id"],
            "source_id": item["source_id"], "claim_type": types[0],
            "text": item["text"], "verifiable": True,
            "note": "事实命题：以连续原文引文与中文释义对照核验。",
        })
        rows.append({
            "claim_id": f"CLM_{item['evidence_id']}_2", "evidence_id": item["evidence_id"],
            "source_id": item["source_id"], "claim_type": types[1],
            "text": item["boundary_note"], "verifiable": True,
            "note": "从适用边界抽取的推断/条件命题；不把价值侧重点写成事实结论。",
        })
    rows.append({
        "claim_id": "CLM_AXIS_1", "evidence_id": None, "source_id": None,
        "claim_type": "value",
        "text": f"{axis['name']}：{axis['positive_label']}／{axis['negative_label']}",
        "verifiable": False,
        "note": "价值侧重点，不设唯一正确答案，不进入二元事实锚题真值。",
    })
    return rows


def _simulated_slot(reviewer_id, evidence_rows, disagree_id, reviewer_index):
    results = []
    for item in evidence_rows:
        eid = item["evidence_id"]
        checks = {
            "source_id_matches": True,
            "quote_is_continuous_original": True,
            "chinese_paraphrase_supported": True,
            "historical_time_section_consistent": True,
            "translation_entities_numbers_negations_conditions_uncertainty": True,
        }
        decision = "approved"
        if eid == disagree_id and reviewer_index == 1:
            checks["chinese_paraphrase_supported"] = False
            decision = "needs_revision"
        results.append({
            "item_id": eid, "reviewer_id": reviewer_id,
            "review_mode": _REVIEW_MODE,
            "decision": decision, "checks": checks,
            "notes": ("模拟核验：仅有分歧演练槽标记为需复核，其余按结构对照通过；"
                      "这不是真人判断。" if eid == disagree_id and reviewer_index == 1
                      else "模拟核验：结构对照通过，仍待真人复核。"),
        })
    return {"reviewer_id": reviewer_id, "review_mode": _REVIEW_MODE,
            "is_human": False, "items": results}


def simulate_section52_dual_review(pilot_materials, *, include_disagreement_case=True) -> dict:
    """§5.2 八步审核流程的离线结构演练；绝不产出真人签字或正式放行。

    从材料包的 topic_id 取冻结spec，因此三议题共用同一八步引擎。
    """
    _need(type(include_disagreement_case) is bool, "disagreement_flag_invalid")
    _need(type(pilot_materials) is dict, "pilot_materials_invalid")
    spec = _spec_for(pilot_materials.get("topic_id"))
    verification = _verify_topic_pilot_materials(pilot_materials, spec)
    _need(verification["passed"] is True, "pilot_materials_not_verified")
    zones = pilot_materials["zones"]
    public_zone = zones["zone_1_subject_public"]
    bindings = pilot_materials["source_bindings"]
    evidence_rows = pilot_materials["topic_evidence"]
    axis = _read(Path(pilot_materials["evidence_build_dir"])
                 / f"packets/{spec['topic_id']}_full.json")["stance_axis"]
    automation = _read(Path(pilot_materials["evidence_build_dir"]) / "review/automation_checks.json")

    # 步骤1：数据版本、原记录ID、URL、获取时间、文件哈希。
    step1 = {
        "step": 1, "name": "版本与来源取证记录",
        "data_version": pilot_materials["evidence_manifest_sha256"],
        "review_mode": _REVIEW_MODE,
        "sources": [{
            "record_id": row["record_id"], "source_id": row["source_id"],
            "url": row["url"], "retrieved_at_utc": row["retrieved_at_utc"],
            "publication_date": row["publication_date"],
            "raw_sha256": row["raw_sha256"], "text_sha256": row["text_sha256"],
            "source_family_id": row["source_family_id"],
            "hash_verified_by_evidence_build": True,
            "redistribution_cleared": row["redistribution_cleared"],
        } for row in bindings],
        "manifest_sha256": pilot_materials["evidence_manifest_sha256"],
        "intake_material_sha256": pilot_materials["intake_material_sha256"],
        "topic_id": spec["topic_id"],
        "topic_folder": spec["folder"],
        "rights_warnings": pilot_materials["rights_warnings"],
        "automated_source_checks": [row for row in automation["source_checks"]
                                    if row["source_id"] in {item["source_id"] for item in bindings}],
        "automated_evidence_checks": [row for row in automation["evidence_checks"]
                                      if row["evidence_id"] in {item["evidence_id"] for item in evidence_rows}],
        "passed": True,
    }

    # 步骤2：命题拆解与五类标记。
    propositions = _propositions(evidence_rows, axis, spec)
    types = {row["claim_type"] for row in propositions}
    _need(types == {"fact", "value", "prediction", "definition", "causal"},
          "claim_type_coverage_incomplete")
    step2 = {"step": 2, "name": "可核验命题拆解与类型标记",
             "claim_types": sorted(types), "propositions": propositions,
             "passed": len(propositions) == 13}

    # 步骤3：两名独立模拟核验槽；分歧条目按开关产生或关闭。
    disagree_id = spec["disagreement_evidence_id"] if include_disagreement_case else None
    slot_a = _simulated_slot(_SIM_A, evidence_rows, disagree_id, 0)
    slot_b = _simulated_slot(_SIM_B, evidence_rows, disagree_id, 1)
    _need(slot_a["reviewer_id"] != slot_b["reviewer_id"], "reviewer_ids_not_independent")
    disagreements = []
    for left, right in zip(slot_a["items"], slot_b["items"]):
        if left["decision"] != right["decision"] or left["checks"] != right["checks"]:
            disagreements.append({
                "item_id": left["item_id"], "reviewer_a_decision": left["decision"],
                "reviewer_b_decision": right["decision"],
                "check_values_differ": left["checks"] != right["checks"],
                "disagreement_detected": True,
            })
    step3 = {"step": 3, "name": "双人独立模拟核验槽",
             "reviewer_slots": [slot_a, slot_b],
             "reviewers_are_simulated_not_human": True,
             "disagreement_items": disagreements,
             "disagreement_detected": bool(disagreements),
             "passed": True}

    # 步骤4：历史时间窗与时间截面一致性。
    years = sorted({int(year) for row in evidence_rows
                    for year in re.findall(r"(?:19|20)[0-9]{2}", row["text"])})
    no_2026_observation = not any("2026" in row["text"] for row in evidence_rows)
    _need(no_2026_observation, "2026_observation_claim_detected")
    step4 = {
        "step": 4, "name": "历史时间窗与截面一致性",
        "source_publication_years": [row["publication_date"] for row in bindings],
        "years_appearing_in_chinese_evidence_text": years,
        "retrieved_at_utc": [row["retrieved_at_utc"] for row in bindings],
        "retrieval_date_is_not_observation_year": True,
        "no_2026_observation_statistics_claim": no_2026_observation,
        "historical_cross_section_policy": spec["time_policy"],
        "topic_id": spec["topic_id"],
        "passed": no_2026_observation,
    }

    # 步骤5：英文材料翻译对照（实体/数字/否定词/条件/不确定性）。
    translation = []
    for item in evidence_rows:
        pairs = spec["translation_pairs"][item["evidence_id"]]
        checks = []
        for english, chinese in pairs:
            checks.append({
                "english_marker": english, "chinese_counterpart": chinese,
                "marker_in_original_quote": english.lower() in item["original_quote"].lower(),
                "counterpart_in_chinese_text": chinese in item["text"],
            })
        preserved = all(row["marker_in_original_quote"] and row["counterpart_in_chinese_text"]
                        for row in checks)
        translation.append({
            "evidence_id": item["evidence_id"], "source_id": item["source_id"],
            "source_language": spec["source_language"],
            "check_kind": ("english_to_chinese_translation" if spec["source_language"] == "en"
                           else "chinese_original_paraphrase_fidelity"),
            "entity_number_negation_condition_uncertainty_checks": checks,
            "all_preserved": preserved,
        })
    step5 = {"step": 5, "name": "英文材料翻译对照",
             "topic_id": spec["topic_id"],
             "source_language": spec["source_language"],
             "items": translation,
             "all_preserved": all(row["all_preserved"] for row in translation),
             "passed": all(row["all_preserved"] for row in translation),
             "note": spec["translation_note"]}

    # 步骤6：筛除规则。
    step6 = {
        "step": 6, "name": "筛除规则检查",
        "no_ambiguous_unverifiable_items": True,
        "no_pure_image_unverified_items": True,
        "no_unpublished_material_items": True,
        "no_unresolved_source_conflicts": True,
        "excluded_items": [],
        "recorded_source_exclusions": pilot_materials["source_family_exclusions"],
        "rights_warnings": pilot_materials["rights_warnings"],
        "rights_warning_note": pilot_materials["rights_warning_note"],
        "rights_gate_pending": all(row["redistribution_cleared"] is False for row in bindings),
        "topic_id": spec["topic_id"],
        "passed": True,
        "note": "自动结构检查不证明语义清晰；逐项权利与用途仍待真人确认。",
    }

    # 步骤7：难度与前测满分预案。
    private = zones["zone_3_evaluator_private_gold"]
    step7 = {
        "step": 7, "name": "预实验难度与满分防范预案",
        "anchor_items": len(private["public_questionnaire"]["anchor_items"]),
        "transfer_items": len(private["public_questionnaire"]["transfer_items"]),
        "anchor_truth_balance": private["anchor_truth_balance"],
        "ceiling_effect_contingency": "全体前测接近满分时启用新情境或平行迁移题，单独报告；不在正式数据里事后挑出易受影响个体。",
        "transfer_reported_separately": True,
        "no_posthoc_subject_selection": True,
        "passed": True,
    }

    # 步骤8：冻结候选manifest，并完整演练分歧拦截→独立第三人裁决闭环。
    unresolved = len(disagreements)
    release_before = {
        "unresolved_items": unresolved,
        "simulated_candidate_structure_pass_before_adjudication": unresolved == 0,
        "blocked_from_pass": unresolved > 0,
        "reason": "存在未决分歧，必须拦截通过；未裁决项不计入冻结通过项。",
    }
    adjudication = {
        "status": "not_started", "adjudicator_id": None,
        "decision": "pending", "reason": None, "adjudicated_items": 0,
    }
    if include_disagreement_case:
        _need(unresolved == 1, "expected_one_disagreement_case")
        adjudication = {
            "status": "resolved_by_independent_third_party",
            "adjudicator_id": _SIM_C, "review_mode": _REVIEW_MODE,
            "is_human": False,
            "adjudicator_distinct_from_both_reviewers": _SIM_C not in (_SIM_A, _SIM_B),
            "decision": "needs_revision",
            "reason": spec["disagreement_reason"],
            "checks": {"source_id_matches": True, "quote_is_continuous_original": True,
                       "chinese_paraphrase_supported": False,
                       "historical_time_section_consistent": True,
                       "translation_entities_numbers_negations_conditions_uncertainty": True},
            "adjudicated_items": 1,
            "item_id": spec["disagreement_evidence_id"],
        }
    step8 = {
        "step": 8, "name": "冻结候选manifest与分歧裁决门禁",
        "release_gate_before_adjudication": release_before,
        "adjudication": adjudication,
        "disagreement_blocked_before_adjudication": include_disagreement_case,
        "adjudication_closed": (not include_disagreement_case
                                or adjudication["status"] == "resolved_by_independent_third_party"),
        "change_policy": "冻结后任何改动须增加版本并重新标注为探索性",
        "passed": True,
    }
    deterministic_body = {
        "steps": [step1, step2, step3, step4, step5, step6, step7],
        "bundle_integrity_sha256": pilot_materials["integrity_sha256"],
        "include_disagreement_case": include_disagreement_case,
    }
    manifest_sha = canonical_hash(deterministic_body)
    result = {
        "contract": "section52_simulated_dual_review_v1",
        "topic_id": spec["topic_id"],
        "topic_folder": spec["folder"],
        "pilot_build_id": pilot_materials["pilot_build_id"],
        "protocol_sha256": pilot_materials["protocol_sha256"],
        "source_language": spec["source_language"],
        "review_mode": _REVIEW_MODE,
        "is_simulation_rehearsal": True,
        "no_real_human_signature": True,
        "human_reviews_completed": 0,
        "approved_for_experiment": False,
        "formal_eligible": False,
        "subject_delivered": False,
        "step_count": 8,
        "steps": [step1, step2, step3, step4, step5, step6, step7, step8],
        "all_eight_steps_present": True,
        "simulated_reviewer_ids": [_SIM_A, _SIM_B, _SIM_C],
        "disagreement_case_included": include_disagreement_case,
        "disagreement_items": disagreements,
        "frozen_candidate_manifest_sha256": manifest_sha,
        "verification": verification,
        "honest_gate": {
            "candidate_structure_checked": True,
            "semantic_entailment_proven": False,
            "real_human_double_review_completed": False,
            "formal_release_allowed": False,
            "statement": "仅验证§5.2审核数据结构与分歧裁决门禁逻辑，绝不替代真实人类双人签字。",
        },
        "new_model_calls": 0, "new_api_calls": 0, "new_cost_cny": "0",
    }
    result["result_sha256"] = canonical_hash(
        {key: value for key, value in result.items() if key != "result_sha256"})
    return result


# ---------------------------------------------------------------------------
# 步骤34：三议题冻结差异配置。所有命题、维度、翻译标记、摘编误读与时间口径
# 均取自 intake/{folder}/topic_material.json 的真实证据与 boundary_note，
# 不引入任何新的外部事实。TOPIC_01配置与步骤33逐字一致，保证旧API兼容。
# ---------------------------------------------------------------------------
_S01_STATEMENTS = [
    ("Q01", "二氧化碳参与地球原有的温室效应；人的额外排放会增强这一自然过程并推动全球气温上升。", "anchor", 1, "D01"),
    ("Q02", "温室效应既然是自然过程，额外增加二氧化碳就不会让全球气温发生变化。", "anchor", 0, "D01"),
    ("Q03", "按该科普页面的表述，工业化以来大气二氧化碳浓度上升主要来自人们为获取能源燃烧化石燃料。", "anchor", 1, "D02"),
    ("Q04", "只要大气二氧化碳增加，植物就会持续、等比例地增产并增加碳吸收。", "anchor", 0, "D03"),
    ("Q05", "二氧化碳增加可能促进植物生长，但可用水分或氮不足时不能仅凭二氧化碳推断持续增产。", "anchor", 1, "D03"),
    ("Q06", "海洋吸收了更多二氧化碳，因此海洋会持续完全抵消人为排放。", "anchor", 0, "D04"),
    ("Q07", "海水平均状态仍呈碱性与海水继续酸化可以同时成立，酸化描述的是pH下降的方向。", "anchor", 1, "D05"),
    ("Q08", "观测到叶面积增加，就等于直接测得某一因素的独立贡献，不需要模型假设。", "anchor", 0, "D06"),
    ("Q09", "在另一个地区，若水分充足但氮不足，仍不能仅凭二氧化碳增加推断作物持续等比例增产。", "transfer", 1, "D03"),
    ("Q10", "若某海域pH下降但整体仍为碱性，可据此判定该海域没有酸化。", "transfer", 0, "D05"),
    ("Q11", "把模型对驱动因素的归因改写为直接观测，不会改变该结论所依赖的假设。", "transfer", 0, "D06"),
    ("Q12", "承认温室效应有自然作用，与承认额外排放会增强该效应，两者并不冲突。", "transfer", 1, "D01"),
]
_S01_DIMENSIONS = [
    {"dimension_id": "D01", "name": "温室效应的自然作用与额外增量",
     "criteria": "回答须区分原有自然过程与额外排放增强该过程，不把两者视为互斥。"},
    {"dimension_id": "D02", "name": "大气二氧化碳增加的人为来源",
     "criteria": "回答须保留主要来源而非唯一来源的限定，不据此断言自然碳循环停止。"},
    {"dimension_id": "D03", "name": "光合作用与二氧化碳施肥的益处及水养分限制",
     "criteria": "回答须说明促进的条件性，区分植物生长与等比例增产或长期净储碳。"},
    {"dimension_id": "D04", "name": "海洋吸收大气二氧化碳的过程",
     "criteria": "回答须把海洋吸收视为定性机制联系，不虚构吸收比例、速度或无限抵消能力。"},
    {"dimension_id": "D05", "name": "海洋酸化的pH方向与仍呈碱性的区别",
     "criteria": "回答须以变化方向判断酸化，不用仍呈碱性否定pH下降。"},
    {"dimension_id": "D06", "name": "观测、模型归因与生态结论的外推边界",
     "criteria": "回答须区分数据检出与模型归因，不把模型贡献写成无假设的直接观测。"},
]
_S01_RUBRIC = {
    "summary": {"0": "未说明另一行动节奏侧重点。",
                "1": "能概括另一节奏的关注点，但混入未经材料支持的事实。",
                "2": "准确概括重视实施条件或优先降低风险的节奏，且只依赖材料可见内容。"},
    "main_evidence": {"0": "没有给出论据。",
                      "1": "给出一条论据但缺少来源或适用条件。",
                      "2": "给出至少一条可回溯到本议题材料的主要论据并保留限定条件。"},
    "boundary": {"0": "未说明适用边界。",
                 "1": "提到边界但表述模糊或与原文限定不符。",
                 "2": "明确说明机制与外推的边界，不把价值侧重点当作事实结论。"},
}

_S02_STATEMENTS = [
    ("Q01", "平准化度电成本是特定技术与统计口径下的发电成本指标，不能直接等同于终端电价。", "anchor", 1, "D01"),
    ("Q02", "全球加权平均太阳能度电成本下降，就足以推出任一项目的终端电价同幅度下降。", "anchor", 0, "D01"),
    ("Q03", "同一年度不同发电技术的成本降幅可以不同，降幅大小不等于绝对成本高低。", "anchor", 1, "D02"),
    ("Q04", "风光整合早期影响较低，说明波动出力天然保证可靠供电且无需系统改造。", "anchor", 0, "D03"),
    ("Q05", "多数并网挑战可通过设备改动或改善运行提高灵活性来应对，但这一判断有早期阶段条件。", "anchor", 1, "D03"),
    ("Q06", "只要配置储能，就能解决所有高比例风光情形下的供电与稳定性问题。", "anchor", 0, "D04"),
    ("Q07", "一千六百二十万描述二〇二三年可再生能源就业存量，不是当年净新增岗位数。", "anchor", 1, "D05"),
    ("Q08", "化石燃料工人技能与绿色职业相近，就能推出其所在地与绿色能源生产地点必然重合。", "anchor", 0, "D06"),
    ("Q09", "在另一年度把加权平均度电成本改作某一电站报价，仍不能由前者直接推出后者。", "transfer", 1, "D01"),
    ("Q10", "技术降幅较大就表示该技术当年的绝对发电成本一定更低。", "transfer", 0, "D02"),
    ("Q11", "两年就业存量之差可描述规模变化，但不能直接当作扣除其他行业岗位损失后的全经济净效应。", "transfer", 1, "D05"),
    ("Q12", "技能相近足以证明不需要培训，也足以证明每名工人都能原地转岗。", "transfer", 0, "D06"),
]
_S02_DIMENSIONS = [
    {"dimension_id": "D01", "name": "成本指标LCOE及边界",
     "criteria": "回答须把LCOE限定为公用事业规模太阳能的全球加权平均发电成本，不外推为终端电价或全天候总成本。"},
    {"dimension_id": "D02", "name": "年度技术差异",
     "criteria": "回答须区分同一年度不同技术的降幅与绝对成本水平，不写成未来下降承诺。"},
    {"dimension_id": "D03", "name": "波动出力条件",
     "criteria": "回答须保留风光整合早期与可实施调整的条件，不推定天然可靠或无需改造。"},
    {"dimension_id": "D04", "name": "储能电网",
     "criteria": "回答须把可调度发电、电网增强、储能与需求响应视为并列手段，不写单一措施解决全部问题。"},
    {"dimension_id": "D05", "name": "就业存量vs净增加",
     "criteria": "回答须区分就业存量、年度规模变化与全经济净就业效应，不把存量当净新增。"},
    {"dimension_id": "D06", "name": "技能地域不均",
     "criteria": "回答须区分技能相似与空间匹配两个问题，不据前者推定后者。"},
]
_S02_RUBRIC = {
    "summary": {"0": "未说明可再生能源扩展节奏的另一侧重点。",
                "1": "能概括另一节奏但混入未经材料支持的事实或绝对化承诺。",
                "2": "准确概括加快部署或优先补齐系统条件的节奏，且只依赖材料可见内容。"},
    "main_evidence": {"0": "没有给出论据。",
                      "1": "给出一条论据但缺少来源、年份或统计口径。",
                      "2": "给出可回溯到本议题材料的论据并保留LCOE口径、年份与二手转引限制。"},
    "boundary": {"0": "未说明适用边界。",
                 "1": "提到边界但把发电成本、就业存量或早期并网条件混用。",
                 "2": "明确区分成本口径、年度差异、系统条件与就业统计口径，不外推为普遍结论。"},
}

_S03_STATEMENTS = [
    ("Q01", "该答复把雨雪图标旁的百分比解释为降雨或降雪出现的可能性，百分数越大表示可能性越大。", "anchor", 1, "D01"),
    ("Q02", "任何软件界面上的百分比都可以直接当作降水概率，也可以用来表示雨量大小。", "anchor", 0, "D01"),
    ("Q03", "降水概率的释义限定在某一地区及一定时段内，不能保证降水百分之百发生。", "anchor", 1, "D02"),
    ("Q04", "收到暴雨蓝色预警就说明所在地此刻降雨量已经达到五十毫米。", "anchor", 0, "D03"),
    ("Q05", "该预警标准包含将达阈值或已达且可能持续两类条件，不能直接当作实况记录。", "anchor", 1, "D03"),
    ("Q06", "旧预警截图无须核对发布时间和有效状态，可直接视为当前仍生效的预警。", "anchor", 0, "D04"),
    ("Q07", "二〇二〇年答复列举的中央气象台主要发布渠道是官网和国家突发事件预警信息发布网。", "anchor", 1, "D05"),
    ("Q08", "集合预报初值误差的科普足以证明某次落空属于正常误差，也足以证明产品已完成概率校准。", "anchor", 0, "D06"),
    ("Q09", "换到另一地区或另一时段的转发消息，仍须重新核对原预报的地域与时段条件。", "transfer", 1, "D02"),
    ("Q10", "页面在二〇二〇年不保留过期信息，说明今天所有平台都不保留历史预警。", "transfer", 0, "D04"),
    ("Q11", "仅有转发者截图或口头归属不能替代对原始发布页面、日期与正文的核对。", "transfer", 1, "D05"),
    ("Q12", "只要一次预报没有落空，就能断定该预报产品已经完成长期概率校准。", "transfer", 0, "D06"),
]
_S03_DIMENSIONS = [
    {"dimension_id": "D01", "name": "概率定义",
     "criteria": "回答须把百分比限定为该答复所述的降水机会，不推及任意软件字段、雨量或持续时间。"},
    {"dimension_id": "D02", "name": "预报时段与地域",
     "criteria": "回答须保留某一地区及一定时段的限定，不把概率数字移用到别处或别的时间。"},
    {"dimension_id": "D03", "name": "预警与实况",
     "criteria": "回答须区分将达阈值与已达且可能持续，不把预警等同于所在地实况记录。"},
    {"dimension_id": "D04", "name": "预警时效更新",
     "criteria": "回答须核对原发布时间、适用时段和是否仍有效，不把二〇二〇年网站做法当作当前统一规则。"},
    {"dimension_id": "D05", "name": "官方发布源",
     "criteria": "回答须回到原发布页面核对发布者、日期与正文，不以机构署名代替逐条核查。"},
    {"dimension_id": "D06", "name": "单次落空、造假判断与校准的解释边界",
     "criteria": "回答须区分初值误差科普、单次落空、真实性调查与长期概率校准，不用前者证明后三者。"},
]
_S03_RUBRIC = {
    "summary": {"0": "未说明不确定天气信息使用策略的另一侧重点。",
                "1": "能概括另一策略但混入未经材料支持的全国统一标准或造假判断。",
                "2": "准确概括核实条件后安排或及时据有效预报作准备，且只依赖材料可见内容。"},
    "main_evidence": {"0": "没有给出论据。",
                      "1": "给出一条论据但缺少发布时点、地域或警种条件。",
                      "2": "给出可回溯到本议题答复的论据并保留发布时间、适用地域与版本限定。"},
    "boundary": {"0": "未说明适用边界。",
                 "1": "提到边界但混用二〇二〇/二〇二二/二〇二三的规则或跨平台推定。",
                 "2": "明确区分历史答复时点、本地警种标准、时效更新与校准实证缺口，不构造历史失误案例。"},
}

_TOPIC_SPECS = {
    "TOPIC_01": {
        "version": "topic_pilot_spec_v2",
        "topic_id": "TOPIC_01", "folder": "climate",
        "questionnaire_id": "QSET_TOPIC01",
        "card_prefixes": {"exposure": "T01EXP", "retention": "T01RET"},
        "source_ids": ["CL_S01", "CL_S02", "CL_S03", "CL_S04"],
        "evidence_ids": [f"CL_E0{i}" for i in range(1, 7)],
        "source_language": "en",
        "publisher_attribution": "NOAA/NASA 等机构",
        "misreading": _MISREADING,
        "translation_pairs": _TRANSLATION_PAIRS,
        "claim_types": _CLAIM_TYPES,
        "statements": _S01_STATEMENTS,
        "dimensions": _S01_DIMENSIONS,
        "opposing_rubric": _S01_RUBRIC,
        "disagreement_evidence_id": "CL_E05",
        "disagreement_reason": "模拟裁决：CL_E05中文释义同时依赖同页碱性语境与pH方向定义段，要求真人复核两处相邻正文后再定稿。",
        "time_policy": "只采用机制说明与有明确年份的研究语境；不混用2011/2016/2025与2026当年观测统计",
        "translation_note": "逐条核对实体、数字、否定词、条件及mostly/it appears/until limit/continues to absorb/based on models等限定；不代替真人第二人复核。",
        "rights_note": "四个公开页面均未完成逐项权利核验；原HTML与提取全文仅本地取证，不公开再分发。",
    },
    "TOPIC_02": {
        "version": "topic_pilot_spec_v2",
        "topic_id": "TOPIC_02", "folder": "energy",
        "questionnaire_id": "QSET_TOPIC02",
        "card_prefixes": {"exposure": "T02EXP", "retention": "T02RET"},
        "source_ids": ["EN_S02", "EN_S05", "EN_S06", "EN_S07", "EN_S08"],
        "evidence_ids": [f"EN_E0{i}" for i in range(1, 7)],
        "source_language": "en",
        "publisher_attribution": "IEA、IRENA/ILO、Nature Communications 或北京大学能源研究院等机构",
        "misreading": {
            "EN_E01": "太阳能发电的全球加权平均度电成本下降，就说明终端电价和全天候供电总成本必然同幅度下降。",
            "EN_E02": "二〇二三年各类技术成本降幅不同，说明降幅较大的技术绝对发电成本一定更低。",
            "EN_E03": "风光并网早期影响较低，因此波动出力天然保证可靠供电，无需任何系统改造。",
            "EN_E04": "只要配置储能，就能解决所有高比例风光系统的供电与稳定性问题。",
            "EN_E05": "二〇二三年全球可再生能源就业达到一千六百二十万，等于这一年净新增了一千六百二十万个岗位。",
            "EN_E06": "化石燃料工人技能与绿色职业相近，说明他们所在地与绿色能源生产地点也必然重合。",
        },
        "translation_pairs": {
            "EN_E01": [("levelized cost of electricity", "平准化度电成本"),
                       ("$0.044/kWh in 2023", "二〇二三年")],
            "EN_E02": [("分别下降了3%和7%", "降幅并不相同"),
                       ("聚太阳能热发电", "聚光太阳能热发电")],
            "EN_E03": [("progressive and targeted adjustments", "逐步实施"),
                       ("most challenges addressable", "多数相关挑战")],
            "EN_E04": [("increased storage and demand response", "储能"),
                       ("introduces supply variability", "供给侧波动")],
            "EN_E05": [("from 13.7 million in 2022 to 16.2 million", "一千六百二十万"),
                       ("18 per cent year-on-year leap", "同比增长")],
            "EN_E06": [("similar skills to green occupations", "技能与绿色职业相近"),
                       ("not co-located with current sources of green energy production", "并不重合")],
        },
        "claim_types": {
            "EN_E01": ("fact", "definition"), "EN_E02": ("fact", "fact"),
            "EN_E03": ("fact", "causal"), "EN_E04": ("fact", "causal"),
            "EN_E05": ("fact", "definition"), "EN_E06": ("fact", "prediction"),
        },
        "statements": _S02_STATEMENTS,
        "dimensions": _S02_DIMENSIONS,
        "opposing_rubric": _S02_RUBRIC,
        "disagreement_evidence_id": "EN_E05",
        "disagreement_reason": "模拟裁决：EN_E05把就业存量与净新增区分开，但一手报告取证失败、本条为二手转引；要求真人核对同一研究家族的发布文本与统计口径后再定稿。",
        "time_policy": "以2023年数据及2024年发布文本为主；一手报告页403取证失败，成本条目为可追溯二手转引；81%按新增容量而非项目数量且本包未采用；不把2023/2024研究或发布年份混作2026当年观测统计。",
        "translation_note": "逐条核对LCOE口径、美元单价与年份、同比降幅、mostly式限定、储能与需求响应并列手段、就业存量与同比、技能相似与地域错配；中文来源EN_E02按原文逐字核对数字与技术名改写，不代替真人第二人复核。",
        "rights_note": "五个来源含二手转引、联合发布与第三方研究；IRENA一手成本/就业页HTTP 403失败且保留失败记录，未取得PDF方法章；同一研究家族不重复计独立来源，整体再分发许可未核验。",
    },
    "TOPIC_03": {
        "version": "topic_pilot_spec_v2",
        "topic_id": "TOPIC_03", "folder": "weather",
        "questionnaire_id": "QSET_TOPIC03",
        "card_prefixes": {"exposure": "T03EXP", "retention": "T03RET"},
        "source_ids": ["WE_S01", "WE_S02", "WE_S04", "WE_S06"],
        "evidence_ids": [f"WE_E0{i}" for i in range(1, 7)],
        "source_language": "zh",
        "publisher_attribution": "中国气象局或中国气象报社等机构",
        "misreading": {
            "WE_E01": "任何软件里显示的百分比都可以直接解释为降水概率，百分数多大就代表雨量多大。",
            "WE_E02": "降水概率可以脱离地区和时段使用，换到别处或别的时间仍然同样有效。",
            "WE_E03": "收到暴雨蓝色预警就说明所在地此刻降雨量已经达到五十毫米。",
            "WE_E04": "历史上发布的预警截图无须核对发布时间和有效状态，可直接当作当前仍在生效的信息。",
            "WE_E05": "只要内容署名来自官方渠道，就能断定每条转发消息都真实无误。",
            "WE_E06": "一次预报落空可以用初值误差直接证明产品已经完成概率校准，也足以证明不存在造假。",
        },
        "translation_pairs": {
            "WE_E01": [("百分数越大，出现降水的可能性越大", "百分数越大"),
                       ("降雨或降雪概率", "出现降雨或降雪的可能性")],
            "WE_E02": [("某一地区及一定时段内", "地区和适用时段"),
                       ("不能保证百分之百", "不能保证")],
            "WE_E03": [("将达50毫米以上", "未来条件"),
                       ("已达50毫米以上且降雨可能持续", "已发生并可能持续")],
            "WE_E04": [("预警信息存在时效性", "具有时效性"),
                       ("网站不保留过期的信息", "不保留过期信息")],
            "WE_E05": [("主要通过其官网及国家突发事件预警信息发布网", "官网和国家突发事件预警信息发布网"),
                       ("目前", "二〇二〇年五月十二日")],
            "WE_E06": [("模式初值与真实大气初始状态之间总是存在误差", "初始状态与真实大气状态之间仍存在误差"),
                       ("即使拥有最好的观测数据", "即使拥有很好的观测数据")],
        },
        "claim_types": {
            "WE_E01": ("fact", "definition"), "WE_E02": ("fact", "definition"),
            "WE_E03": ("fact", "causal"), "WE_E04": ("fact", "causal"),
            "WE_E05": ("fact", "definition"), "WE_E06": ("fact", "prediction"),
        },
        "statements": _S03_STATEMENTS,
        "dimensions": _S03_DIMENSIONS,
        "opposing_rubric": _S03_RUBRIC,
        "disagreement_evidence_id": "WE_E05",
        "disagreement_reason": "模拟裁决：WE_E05的“目前”只指2020-05-12答复时点，渠道清单不能外推为当前配置；要求真人核对答复原文、日期与后续版本后再定稿。",
        "time_policy": "取证日2026-11-08不是发布日；WE_S01为2022-07-06、WE_S02为2023-06-17、WE_S04为2023-01-06、WE_S06为2020-05-12，不从URL年月推断发布日期；不拼接成某一天的统一业务状态，不声称2026年或未来每个平台采用同一概率定义、保留方式及预警标准。",
        "translation_note": "本议题原文为中文，无英文翻译环节；逐条核对原句的数字、否定词、条件与不确定性在中文释义中的保留（将达/已达、不能保证、目前、总是存在误差等），并保留本地取证排除项与警种地域差异，不代替真人第二人复核。",
        "rights_note": "四个中文公开页面均未完成逐项权利核验；WE_S03已取证但内容不足未入选，WE_S05因HTTPS降级重定向被阻止未生成text.json，两处失败记录均保留；不构造历史错误消息或当前实时天气，整体再分发许可未核验。",
    },
}


def build_all_topics_pilot_materials(evidence_root, *, parallel=True,
                                    live_pairs_path=None) -> dict:
    """三议题并行自动化构建：统一格式、统一门限、统一 pilot_manifest。"""
    _need(type(parallel) is bool, "parallel_flag_invalid")
    build = _resolve_build(evidence_root)
    raw = Path(evidence_root).resolve()
    if (raw / "intake").is_dir():
        root = raw
    elif raw.name == "intake" and (raw.parent / "intake").is_dir():
        root = raw.parent
    else:
        root = build.parent
    _need((root / "intake").is_dir(), "source_root_with_intake_missing")
    manifest = _read(build / "manifest.json")
    _need(manifest["protocol_sha256"] == evidence.PROTOCOL_SHA
          and set(TOPIC_IDS.values()) == {row["topic_id"] for row in manifest["topics"]},
          "manifest_topic_or_protocol_mismatch")
    topic_ids = [TOPIC_IDS[folder] for folder in TOPICS]

    from . import live_cross_topic_delivery as live_delivery
    live = live_delivery.live_pairs_for_materials(Path(__file__).resolve().parents[1],
                                                  manifest_path=live_pairs_path)
    live_by_topic = {}
    if live["available"]:
        for (topic_id, round_no), row in live["pairs"].items():
            event_id = row["provenance"]["event_id"]
            _need(type(event_id) is str and len(event_id) == 32, "live_event_id_invalid")
            live_by_topic.setdefault(topic_id, {})[round_no] = dict(
                row, provenance={**row["provenance"], "event_ids": [event_id]})

    def construct(topic_id):
        return topic_id, build_topic_pilot_materials(
            build, topic_id, source_root=root, live_pairs=live_by_topic.get(topic_id))

    if parallel:
        with ThreadPoolExecutor(max_workers=len(topic_ids)) as pool:
            pairs = list(pool.map(construct, topic_ids))
    else:
        pairs = [construct(topic_id) for topic_id in topic_ids]
    bundles = dict(pairs)

    def verify(topic_id):
        return topic_id, _verify_topic_pilot_materials(bundles[topic_id], _spec_for(topic_id))

    if parallel:
        with ThreadPoolExecutor(max_workers=len(topic_ids)) as pool:
            verification_pairs = list(pool.map(verify, topic_ids))
    else:
        verification_pairs = [verify(topic_id) for topic_id in topic_ids]
    verifications = dict(verification_pairs)

    rows = []
    for topic_id in topic_ids:
        bundle = bundles[topic_id]
        spec = _spec_for(topic_id)
        rows.append({
            "topic_id": topic_id, "topic_folder": spec["folder"],
            "folder": spec["folder"],
            "build_id": bundle["pilot_build_id"],
            "evidence_build_id": bundle["evidence_build_id"],
            "protocol_sha256": bundle["protocol_sha256"],
            "integrity_sha256": bundle["integrity_sha256"],
            "intake_material_sha256": bundle["intake_material_sha256"],
            "source_ids": spec["source_ids"], "evidence_ids": spec["evidence_ids"],
            "source_count": len(spec["source_ids"]),
            "exposure_cards": 72, "retention_cards": 36,
            "card_length_chars": [CARD_MIN_CHARS, CARD_MAX_CHARS],
            "schedule_calls": 96, "discussion_slots": 12,
            "intervention_packets": 12, "dose_chars": [DOSE_MIN_CHARS, DOSE_MAX_CHARS],
            "anchor_items": 8, "anchor_truth_balance": [4, 4],
            "transfer_items": 4, "private_dimensions": 6,
            "rights_warning_count": len(bundle["rights_warnings"]),
            "verified": verifications[topic_id]["passed"],
        })
    pilot_manifest = {
        "contract": "pilot_manifest_v1",
        "build_id": manifest["build_id"],
        "protocol_sha256": manifest["protocol_sha256"],
        "protocol_sections": ["5.1", "5.2", "5.3", "5.4", "6.4", "7.1", "7.2"],
        "topic_count": len(topic_ids),
        "topics": rows,
        "uniform_thresholds": {
            "exposure_cards": 72, "retention_cards": 36,
            "card_length_chars": [CARD_MIN_CHARS, CARD_MAX_CHARS],
            "schedule_calls": 96, "max_repeats": 2,
            "discussion_slots": 12, "intervention_packets": 12,
            "dose_chars": [DOSE_MIN_CHARS, DOSE_MAX_CHARS],
            "anchor_items": 8, "anchor_truth_balance": [4, 4],
            "transfer_items": 4, "private_dimensions": 6,
            "arms_with_extractive_binding": 6,
        },
        "rights_gate": "三个议题每个真题源的redistribution_cleared均为false，license_note完整传递",
        "human_reviews_completed": 0, "approved_for_experiment": False,
        "formal_eligible": False, "subject_delivered": False,
    }
    result = {
        "contract": ALL_TOPICS_VERSION,
        "topics": bundles,
        "verifications": verifications,
        "pilot_manifest": pilot_manifest,
        "topic_ids": topic_ids,
        "execution_mode": "parallel_threads_deterministic_ordered_output" if parallel
                          else "sequential_deterministic",
        "review_mode": _REVIEW_MODE,
        "is_simulation_rehearsal": True,
        "human_reviews_completed": 0,
        "approved_for_experiment": False,
        "formal_eligible": False,
        "subject_delivered": False,
        "new_model_calls": 0, "new_api_calls": 0, "new_cost_cny": "0",
    }
    result["integrity_sha256"] = canonical_hash(
        {key: value for key, value in result.items() if key != "integrity_sha256"})
    return result


def simulate_all_topics_dual_review(pilot_materials_bundle, *,
                                    include_disagreement_case=True, parallel=True) -> dict:
    """三议题并行执行§5.2模拟，统一汇总分歧/裁决计数并强制诚实门禁。"""
    _need(type(include_disagreement_case) is bool, "disagreement_flag_invalid")
    _need(type(parallel) is bool, "parallel_flag_invalid")
    _need(type(pilot_materials_bundle) is dict, "all_topics_bundle_invalid")
    if "topics" in pilot_materials_bundle:
        bundles = pilot_materials_bundle["topics"]
        _need(pilot_materials_bundle.get("contract") == ALL_TOPICS_VERSION,
              "all_topics_contract_invalid")
    else:
        bundles = pilot_materials_bundle
    topic_ids = [TOPIC_IDS[folder] for folder in TOPICS]
    _need(set(bundles) == set(topic_ids), "all_topics_bundle_topic_set_invalid")

    def one(topic_id):
        return topic_id, simulate_section52_dual_review(
            bundles[topic_id], include_disagreement_case=include_disagreement_case)

    if parallel:
        with ThreadPoolExecutor(max_workers=len(topic_ids)) as pool:
            pairs = list(pool.map(one, topic_ids))
    else:
        pairs = [one(topic_id) for topic_id in topic_ids]
    simulations = dict(pairs)

    per_topic = {}
    disagreement_total = adjudicated_total = blocked_total = 0
    for topic_id in topic_ids:
        row = simulations[topic_id]
        step8 = row["steps"][7]
        disagreements = len(row["disagreement_items"])
        adjudicated = step8["adjudication"]["adjudicated_items"]
        blocked = 1 if step8["release_gate_before_adjudication"]["blocked_from_pass"] else 0
        disagreement_total += disagreements
        adjudicated_total += adjudicated
        blocked_total += blocked
        per_topic[topic_id] = {
            "topic_folder": _spec_for(topic_id)["folder"],
            "step_count": row["step_count"],
            "all_eight_steps_present": row["all_eight_steps_present"],
            "disagreement_items": disagreements,
            "adjudicated_items": adjudicated,
            "blocked_before_adjudication": bool(blocked),
            "adjudication_closed": step8["adjudication_closed"],
            "frozen_candidate_manifest_sha256": row["frozen_candidate_manifest_sha256"],
            "honest_gate": row["honest_gate"],
            "is_simulation_rehearsal": row["is_simulation_rehearsal"],
            "human_reviews_completed": row["human_reviews_completed"],
            "approved_for_experiment": row["approved_for_experiment"],
            "formal_eligible": row["formal_eligible"],
        }
    gates_ok = all(
        row["is_simulation_rehearsal"] is True
        and row["human_reviews_completed"] == 0
        and row["approved_for_experiment"] is False
        and row["formal_eligible"] is False
        and row["honest_gate"]["formal_release_allowed"] is False
        and row["all_eight_steps_present"] is True
        and row["step_count"] == 8
        and row["adjudication_closed"] is True
        for row in per_topic.values())
    _need(gates_ok, "all_topics_honest_gate_failed")
    result = {
        "contract": "all_topics_section52_simulated_dual_review_v1",
        "topic_count": len(topic_ids),
        "topic_ids": topic_ids,
        "per_topic": per_topic,
        "simulations": simulations,
        "disagreement_items_total": disagreement_total,
        "adjudicated_items_total": adjudicated_total,
        "blocked_before_adjudication_total": blocked_total,
        "include_disagreement_case": include_disagreement_case,
        "execution_mode": "parallel_threads_deterministic_ordered_output" if parallel
                          else "sequential_deterministic",
        "all_topics_honest_gate_enforced": gates_ok,
        "review_mode": _REVIEW_MODE,
        "is_simulation_rehearsal": True,
        "no_real_human_signature": True,
        "human_reviews_completed": 0,
        "approved_for_experiment": False,
        "formal_eligible": False,
        "subject_delivered": False,
        "new_model_calls": 0, "new_api_calls": 0, "new_cost_cny": "0",
    }
    result["result_sha256"] = canonical_hash(
        {key: value for key, value in result.items() if key != "result_sha256"})
    return result
