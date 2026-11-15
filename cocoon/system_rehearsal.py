"""步骤35：三议题全链路离线系统压力预演（工程链路合龙，不产生实验结果）。

本模块把步骤34的三议题标准化材料包、步骤30/31的测量与双盲回填、步骤32的
网络级统计检验，连同 scheduling/runtime/metrics/contracts 串成一次端对端的
**纯离线工程演练**。所有受试回答、盲评与统计数字都由确定性合成规则生成，
显式标注 synthetic_rehearsal_response / is_demo / rehearsal_mode；绝不冒充
真实模型预实验、真实送达、真人审核或正式实验效应结论。

- 不联网、不调用模型、不写文件、不读金标准来生成受试回答。
- 不修改 engineering.dry_run 及步骤28–34任何产物；旧 SYNTHETIC 夹具 dry-run 保留。
- 三区隔离、六臂全量合规可见文本、T1常规发言→mirror、双盲匿名回填、
  2×2曝光对比、槽位随机化置换＋Holm 校正全部走既有公共 API。
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import re

from . import analysis, measurement, metrics, pilot_materials as pm, runtime, scheduling
from .contracts import STAGE_SECTIONS, parse_stage_output, validate_packet

__all__ = ["run_pilot_system_rehearsal", "verify_pilot_system_rehearsal"]

REHEARSAL_VERSION = "pilot_system_rehearsal_v1"
REHEARSAL_MODE = "offline_system_stress_rehearsal"
VERIFY_VERSION = "pilot_system_rehearsal_verification_v1"
NOTICE = (
    "三议题全链路离线系统压力预演；全部回答、盲评和统计数值均为确定性合成演练数据，"
    "不是真实模型输出、不是真实送达、不是真人审核、不是预实验或正式实验结果。"
)

TOPIC_ORDER = tuple(pm.TOPIC_IDS[folder] for folder in pm.TOPICS)
ARMS = tuple(pm.ARMS)
ENVIRONMENTS = tuple(pm.ENVIRONMENTS)
ROUNDS = tuple(pm.ROUNDS)
EXPOSURE_ROUNDS = pm.EXPOSURE_ROUNDS
RETENTION_ROUNDS = pm.RETENTION_ROUNDS
PROTOCOL_SECTIONS = ("6", "7", "8", "9", "10", "11", "13.4", "15.1")

_CN_DIGIT = "零一二三四五六七八九"
_HONEST_GATES = {
    "rehearsal_mode": REHEARSAL_MODE,
    "is_demo": True,
    "synthetic_rehearsal_response": True,
    "actual_model_calls": 0,
    "actual_api_calls": 0,
    "new_cost_cny": "0",
    "human_reviews_completed": 0,
    "approved_for_experiment": False,
    "formal_eligible": False,
    "formal_launch_enabled": False,
    "subject_delivered": False,
    "statistical_outputs_are_synthetic": True,
}

# 与 pilot_materials 的同名门限独立复刻一份，用于本轮自己的三区泄漏扫描。
_GOLD_TEXT = re.compile(
    r"gold_answers|gold_bundle|opposing_rubric|dimension_id|六维清单|六维|"
    r"金标准|标准答案|真值|\btruth\b|\bis_accurate\b", re.I)
_FORBIDDEN_PUBLIC_KEYS = frozenset({
    "gold_answers", "truth", "is_accurate", "stance", "label", "labels",
    "analysis", "justification", "dimensions", "opposing_rubric",
    "private_only", "mastered", "recognized_gap", "approved_for_experiment",
    "answer", "answers", "partition",
})
_PUBLIC_CARD_KEYS = frozenset({
    "card_id", "topic_id", "text", "source_family_id", "source_url",
    "transformation_type", "is_demo",
})


def _need(condition, code):
    if not condition:
        raise ValueError("system_rehearsal:" + code)


def _sha(value):
    return runtime.canonical_hash(value)


def _bytes_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":"))


def _walk_keys(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= _walk_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= _walk_keys(item)
    return found


def _zone_leak_scan(bundle):
    """独立重扫受试公共区与助手证据区，确认零金标准/标签泄漏。"""
    zones = bundle["zones"]
    public = zones["zone_1_subject_public"]
    assistant = zones["zone_2_assistant_evidence"]
    subject_payload = {
        "public_exposure_cards": public["public_exposure_cards"],
        "public_retention_cards": public["public_retention_cards"],
        "discussion_slots": public["discussion_slots"],
        "blank_discussion": public["blank_discussion"],
        "intervention_visible_texts": [
            public["intervention_packets"][arm][str(r)]["visible_text"]
            for arm in ARMS for r in ROUNDS
        ],
    }
    forbidden_keys = _walk_keys(subject_payload) | _walk_keys(assistant)
    leaked_keys = sorted(forbidden_keys & _FORBIDDEN_PUBLIC_KEYS)
    leaked_text = bool(_GOLD_TEXT.search(_dump(subject_payload)) or
                       _GOLD_TEXT.search(_dump(assistant)))
    zone_hashes = {name: _sha(zones[name]) for name in zones}
    return {
        "forbidden_keys_found": leaked_keys,
        "gold_text_leak": leaked_text,
        "zone_count": len(zones),
        "zone_hashes": zone_hashes,
        "three_zone_hashes_distinct": len(set(zone_hashes.values())) == 3,
        "private_zone_only": zones["zone_3_evaluator_private_gold"]["private_only"] is True,
        "leak_free": (not leaked_keys and not leaked_text
                      and len(set(zone_hashes.values())) == 3
                      and zones["zone_3_evaluator_private_gold"]["private_only"] is True),
    }


def _demo_questionnaire(private_zone):
    """冻结候选问卷/金标准的演练副本：只把 is_demo 置真，其余内容逐字不变。"""
    q = deepcopy(private_zone["public_questionnaire"])
    gold = deepcopy(private_zone["private_gold_bundle"])
    q["is_demo"] = True
    gold["is_demo"] = True
    checked_q = measurement.validate_public_questionnaire(q)
    checked_gold = measurement.validate_private_gold_bundle(gold, checked_q)
    _need(checked_q["is_demo"] is True and checked_gold["bundle"]["is_demo"] is True,
          "demo_measurement_identity_invalid")
    _need(checked_gold["private_only"] is True and checked_gold["formal_eligible"] is False,
          "demo_gold_zone_invalid")
    return checked_q, checked_gold


def _frozen_packet(bundle):
    """只读加载该议题 round_1 冻结证据包，mirror 契约使用它而非自造夹具。"""
    meta = bundle["zones"]["zone_2_assistant_evidence"]["packets"]["round_1"]
    path = Path(bundle["evidence_build_dir"]) / meta["path"]
    _need(path.is_file(), "mirror_packet_missing")
    raw = path.read_bytes()
    _need(hashlib.sha256(raw).hexdigest() == meta["byte_sha256"], "mirror_packet_hash_mismatch")
    packet = validate_packet(runtime._parse_json(raw), allow_demo=True)
    _need(_sha(packet) == meta["canonical_sha256"], "mirror_packet_canonical_mismatch")
    _need(packet["topic_id"] == bundle["topic_id"], "mirror_packet_topic_mismatch")
    return packet


def _demo_card(card):
    """受试可见卡的演练副本：只把送达标记改为 is_demo=True，正文与ID逐字不变。"""
    out = deepcopy(card)
    _need(set(out) == _PUBLIC_CARD_KEYS, "public_card_fields_changed")
    out["is_demo"] = True
    return out


def _topic_tag(topic_id):
    return "T" + topic_id.rsplit("_", 1)[-1]


def _normalized_state(state):
    """去掉分支身份/时间推进字段，只比较处理前真实认知与材料状态。"""
    # 身份/时点/环境标签属于设计元数据；比较的是处理前的画像、记忆、消息、
    # 网络、曝光历史、常规发言与队列等真实可变状态。
    dropped = ("world_id", "parent_world_id", "arm", "environment", "slot",
               "response_seed", "response_random_state", "timepoint", "round",
               "logical_seq", "intervention_history")
    return {key: deepcopy(value) for key, value in state.items() if key not in dropped}


def _baseline_state(bundle, population, network, topic_id, block_id, block_seed, tag):
    """§8.1 快照字段：画像、消息/记忆、网络、随机状态、材料清单与各种队列。"""
    return {
        "world_id": f"W_{tag}_{block_id}_BASE",
        "block_id": block_id,
        "topic_id": topic_id,
        "seed": block_seed,
        "environment": None,
        "arm": None,
        "timepoint": "T0",
        "round": 0,
        "logical_seq": 0,
        "is_demo": True,
        "system_prompt": "请仅依据已送达的公开材料作答，回答本身不写回记忆。",
        "agents": deepcopy(population),
        "messages": {agent["agent_id"]: [] for agent in population},
        "network": deepcopy(network),
        "memory_summaries": {agent["agent_id"]: [] for agent in population},
        "truncation_records": [],
        "queues": [],
        "recommendation_buffer": [],
        "pending_tasks": [],
        "platform_database": None,
        "platform_integration": "not_connected",
        "exposure_history": [],
        "routine_speech_history": [],
        "intervention_history": [],
        "prepared_inputs": [],
        "random_state": json.loads(json.dumps(random.Random(block_seed).getstate())),
        "material_manifest_sha256": bundle["integrity_sha256"],
        "config_hash": _sha({"topic_id": topic_id, "block_id": block_id, "seed": block_seed}),
        "measurement_writes_to_memory": False,
    }


def _routine_text(round_no):
    return (f"第{_CN_DIGIT[round_no]}轮我想核对这条公开摘编的适用条件与原文限定，"
            "并保留尚待查证的部分。")


def _probability(spec, item_id):
    """确定性合成概率：只使用已见卡ID、题号与预设演练条件标签，绝不读金标准。"""
    digest = _sha([spec["topic_id"], spec["timepoint"], spec["condition_tag"],
                   item_id, spec["visible_card_ids"]])
    return int(digest[:12], 16) / float(0xFFFFFFFFFFFF)


def _synthetic_measurement(spec, questionnaire):
    """附录B严格三字段；答案只引用受试确实见过的卡，不补字、不自评分。"""
    visible = list(spec["visible_card_ids"])
    cites = visible[:1]
    answers = []
    for index, item in enumerate(questionnaire["anchor_items"] + questionnaire["transfer_items"], 1):
        answers.append({
            "item_id": item["item_id"],
            "p_true": round(_probability(spec, item["item_id"]), 6),
            "brief_reason": f"据已见材料第{index}条，仍待核对边界。",
            "cited_card_ids": list(cites),
        })
    return {
        "answers": answers,
        "evidence_notes": [{
            "known_evidence": "已见的公开摘编只覆盖其写出的机制与限定范围。",
            "remaining_question": "尚需核对这条摘编的适用条件与原文限定。",
        }],
        "opposing_view": {
            "summary": "另一观点更重视实施条件与成本。",
            "main_evidence": "其论据来自对所附公开材料限定条件的强调。",
            "boundary": "该观点只适用于材料写明的范围。",
        },
    }


def _synthetic_blind_evaluation(packet, spec):
    """离线合成盲评：引文必须是原notes的精确子串，不输出条件/时点信息。"""
    quote = packet["evidence_notes"][0]["known_evidence"]
    digest = _sha([spec["topic_id"], spec["timepoint"], spec["condition_tag"],
                   packet["blind_sample_id"]])
    flags = []
    for index, dim in enumerate(packet["dimensions"]):
        mastered = bool(int(digest[index * 2], 16) % 3 == 0)
        gap = bool(int(digest[index * 2 + 1], 16) % 3 == 1)
        flags.append((mastered, gap))
    return {
        "blind_sample_id": packet["blind_sample_id"],
        "dimensions": [{
            "dimension_id": dim["dimension_id"], "mastered": mastered,
            "recognized_gap": gap,
            "evidence_quote": quote if mastered or gap else None,
        } for dim, (mastered, gap) in zip(packet["dimensions"], flags)],
        "opposing_scores": {
            "summary": int(digest[12], 16) % 3,
            "main_evidence": int(digest[13], 16) % 3,
            "boundary": int(digest[14], 16) % 3,
            "rationale": "仅依据所给公开文字与评分量规作离线合成评定。",
        },
    }


def _mirror_output(packet, user_quote):
    evidence_id = packet["evidence"][0]["evidence_id"]
    sections = [{"name": name, "items": []} for name in STAGE_SECTIONS["mirror"]]
    sections[0]["items"] = [{
        "text": "该常规发言需在已见材料的适用边界内理解，不额外引入事实。",
        "citations": [evidence_id],
        "epistemic_status": "evidence_based",
        "user_quote": user_quote,
    }]
    return {"schema_version": "1.0", "stage": "mirror",
            "packet_id": packet["packet_id"], "sections": sections}


def _check_context(ctx, counter):
    """测量上下文必须只读：状态哈希不变、不写回记忆、不具备正式资格。"""
    _need(ctx["world_state_before_sha256"] == ctx["world_state_after_sha256"],
          "measurement_mutated_world")
    _need(ctx["measurement_writes_to_memory"] is False and ctx["formal_eligible"] is False,
          "measurement_writeback_or_eligibility_invalid")
    counter["checks"] += 1


_MEASUREMENT_VIEW_CONTROLLER_ONLY = frozenset((
    "network", "routine_speech_history", "intervention_history", "random_state",
    "prepared_inputs", "memory_summaries", "truncation_records", "queues",
    "recommendation_buffer", "pending_tasks", "platform_database",
    "material_manifest_sha256", "config_hash", "parent_world_id",
))


def _prepare_contexts(state, population, questionnaire, *, audit_index=0,
                      full_audit=False, counter, timepoint):
    """按 §8 建立只读测量副本，逐受试准备上下文。

    每位受试获得只含本人已送达事件的只读测量副本；控制器专用字段（网络、
    随机状态、常规发言历史等）不进入测量上下文。audit=True 时额外保留一次
    **完整状态**调用，现场核验「他人/未送达事件不入白名单」。完整状态审计
    按议题-区组抽样，而不是逐世界重复；步骤30的单元测试已穷尽该排除逻辑。
    """
    visible = {}
    state_before = _sha(state) if full_audit else None
    audit_agent_id = population[audit_index]["agent_id"]
    own_ids = {card["card_id"] for event in state["exposure_history"]
               if event["agent_id"] == audit_agent_id for card in event["cards"]}
    for index, agent in enumerate(population):
        agent_id = agent["agent_id"]
        if full_audit and index == audit_index:
            target = state
            counter["full_state_audits"] += 1
        else:
            target = {key: value for key, value in state.items()
                      if key not in _MEASUREMENT_VIEW_CONTROLLER_ONLY}
            target["exposure_history"] = [event for event in state["exposure_history"]
                                          if event["agent_id"] == agent_id]
        ctx = measurement.prepare_subject_measurement_context(
            target, agent_id, questionnaire, allow_demo=True)
        _check_context(ctx, counter)
        _need(ctx["timepoint"] == timepoint, "measurement_timepoint_invalid")
        visible[agent_id] = list(ctx["visible_card_ids"])
        if full_audit and index == audit_index:
            _need(set(ctx["visible_card_ids"]) == own_ids,
                  "full_state_exclusion_audit_failed")
    if full_audit:
        _need(_sha(state) == state_before, "full_state_audit_mutated_world")
    return visible


def _parameter_check(blocks_per_topic, agents_per_world, seed, bootstrap_reps, permutation_reps):
    for name, value in (("blocks_per_topic", blocks_per_topic), ("agents_per_world", agents_per_world),
                        ("seed", seed), ("bootstrap_reps", bootstrap_reps),
                        ("permutation_reps", permutation_reps)):
        _need(type(value) is int, name + "_not_integer")
    _need(1 <= blocks_per_topic <= 8, "blocks_per_topic_invalid")
    try:
        scheduling._check_population_size(agents_per_world)
    except (TypeError, ValueError):
        raise ValueError("system_rehearsal:agents_per_world_invalid") from None
    _need(bootstrap_reps > 0 and permutation_reps > 0, "repetitions_invalid")
    _need(0 <= seed < 2 ** 63, "seed_invalid")


def run_pilot_system_rehearsal(evidence_root, *, blocks_per_topic=2, agents_per_world=8,
                               seed=20261109, bootstrap_reps=200, permutation_reps=200) -> dict:
    """三议题 × 区组 × A/B/C/D × 六臂 × 四时点的端到端离线系统压力预演。"""
    _parameter_check(blocks_per_topic, agents_per_world, seed, bootstrap_reps, permutation_reps)
    # ---------------------------------------------------------------- 阶段1
    materials_bundle = pm.build_all_topics_pilot_materials(evidence_root)
    _need(materials_bundle["contract"] == pm.ALL_TOPICS_VERSION
          and materials_bundle["formal_eligible"] is False
          and materials_bundle["human_reviews_completed"] == 0, "materials_bundle_invalid")
    _need(tuple(materials_bundle["topic_ids"]) == TOPIC_ORDER, "materials_topic_order_invalid")

    topic_materials = {}
    questionnaires, gold_bundles, frozen_packets = {}, {}, {}
    for topic_id in TOPIC_ORDER:
        bundle = materials_bundle["topics"][topic_id]
        verified = pm.verify_topic_pilot_materials(bundle, topic_id)
        _need(verified["passed"] is True, "topic_materials_verification_failed")
        leak = _zone_leak_scan(bundle)
        _need(leak["leak_free"], "three_zone_gold_leak")
        public = bundle["zones"]["zone_1_subject_public"]
        private = bundle["zones"]["zone_3_evaluator_private_gold"]
        q, gold = _demo_questionnaire(private)
        questionnaires[topic_id], gold_bundles[topic_id] = q, gold
        frozen_packets[topic_id] = _frozen_packet(bundle)
        dose_rows = []
        for arm in ARMS:
            for round_no in ROUNDS:
                item = public["intervention_packets"][arm][str(round_no)]
                visible = item["visible_text"]
                _need(type(visible) is str and bool(visible.strip()), "intervention_visible_text_empty")
                _need(pm.DOSE_MIN_CHARS <= len(visible) <= pm.DOSE_MAX_CHARS,
                      "intervention_visible_text_out_of_range")
                _need(item["char_count"] == len(visible)
                      and item["response_opportunities"] == 1
                      and item["retention_cards_used"] is False
                      and item["contains_treatment_label"] is False,
                      "intervention_contract_invalid")
                dose_rows.append({
                    "topic_id": topic_id, "arm": arm, "round": round_no,
                    "char_count": len(visible), "visible_text_sha256": _bytes_sha(visible),
                    "nonempty": True, "in_range": True,
                    "response_opportunities": 1,
                    "extractive_control_constraint_satisfied":
                        item["extractive_control_constraint_satisfied"] is True,
                })
        topic_materials[topic_id] = {
            "topic_id": topic_id,
            "topic_folder": bundle["topic_folder"],
            "pilot_build_id": bundle["pilot_build_id"],
            "integrity_sha256": bundle["integrity_sha256"],
            "protocol_sha256": bundle["protocol_sha256"],
            "material_verification_passed": verified["passed"],
            "exposure_cards": len(public["exposure_cards"]),
            "retention_cards": len(public["retention_cards"]),
            "public_exposure_cards": len(public["public_exposure_cards"]),
            "public_retention_cards": len(public["public_retention_cards"]),
            "discussion_slots": len(public["discussion_slots"]),
            "intervention_packets": len(dose_rows),
            "questionnaire_id": q["questionnaire_id"],
            "questionnaire_sha256": gold["questionnaire_sha256"],
            "gold_bundle_sha256": gold["bundle_sha256"],
            "anchor_items": len(q["anchor_items"]),
            "transfer_items": len(q["transfer_items"]),
            "private_dimensions": len(gold["bundle"]["dimensions"]),
            "zone_leak_scan": leak,
            "dose_rows": dose_rows,
            "mirror_packet_id": frozen_packets[topic_id]["packet_id"],
        }

    # ------------------------------------------------- 阶段2/3：世界、曝光、分支
    event_seq = 0
    speech_seq = 0
    worlds, mirror_checks, branches = [], [], []
    samples = []
    measurement_audit = {"checks": 0, "full_state_audits": 0}
    allocation_blocks = []
    intervention_delivery_count = 0
    retention_delivery_count = 0

    for topic_index, topic_id in enumerate(TOPIC_ORDER):
        bundle = materials_bundle["topics"][topic_id]
        public = bundle["zones"]["zone_1_subject_public"]
        q = questionnaires[topic_id]
        packet = frozen_packets[topic_id]
        tag = _topic_tag(topic_id)
        exposure_cards = public["exposure_cards"]
        retention_cards = public["retention_cards"]
        exposure_public = {row["card_id"]: row for row in public["public_exposure_cards"]}
        retention_public = {row["card_id"]: row for row in public["public_retention_cards"]}
        slots = public["discussion_slots"]
        exposure_slots = {row["round"]: row for row in slots if row["phase"] == "exposure"}
        retention_slots = {row["round"]: row for row in slots if row["phase"] == "retention"}
        _need(len(exposure_slots) == EXPOSURE_ROUNDS and len(retention_slots) == RETENTION_ROUNDS,
              "discussion_slot_phase_count_invalid")
        _need(not (set(exposure_public) & set(retention_public)), "phase_card_pool_overlap")

        for block_index in range(blocks_per_topic):
            block_id = f"B{block_index + 1:02d}"
            block_seed = seed + topic_index * 10007 + block_index * 101
            population = scheduling.make_population(agents_per_world, block_seed)
            network = scheduling.make_network(population, block_seed + 1)
            base_hash = _sha({"agents": population, "network": network})
            baseline = _baseline_state(bundle, population, network, topic_id,
                                       block_id, block_seed, tag)
            baseline_core_hash = _sha(_normalized_state(baseline))
            env_states = {}
            world_records = []

            for environment in ENVIRONMENTS:
                world_id = f"W_{tag}_{block_id}_{environment}"
                state = runtime.branch_state(baseline, world_id)
                state["environment"] = environment
                state["arm"] = None
                _need(_sha({"agents": state["agents"], "network": state["network"]}) == base_hash,
                      "environment_base_population_network_mismatch")
                initial_core_hash = _sha(_normalized_state(state))
                _need(initial_core_hash == baseline_core_hash,
                      "environment_initial_state_hash_mismatch")

                # T0：接触材料前的测量上下文（可见卡为空）。
                t0_visible = _prepare_contexts(state, population, q,
                                               counter=measurement_audit, timepoint="T0")
                for agent in population:
                    samples.append({
                        "topic_id": topic_id, "block_id": block_id, "world_id": world_id,
                        "agent_id": agent["agent_id"], "environment": environment, "arm": None,
                        "timepoint": "T0", "condition_tag": f"E{environment}",
                        "visible_card_ids": list(t0_visible[agent["agent_id"]]),
                    })

                schedules = {}
                for index, agent in enumerate(population):
                    schedules[agent["agent_id"]] = scheduling.schedule_cards(
                        exposure_cards, agent["initial_stance"], environment,
                        EXPOSURE_ROUNDS, (block_seed + index) % 6, max_repeats=2)
                manipulation = []
                for round_no in range(1, EXPOSURE_ROUNDS + 1):
                    slot = exposure_slots[round_no]
                    _need(slot["frozen"] is True and slot["generated_live"] is False,
                          "discussion_slot_not_frozen")
                    for index, agent in enumerate(population):
                        batch = schedules[agent["agent_id"]][round_no - 1]
                        _need(len(batch) == 6, "exposure_round_card_count_invalid")
                        delivered = [_demo_card(exposure_public[card["card_id"]]) for card in batch]
                        event_seq += 1
                        state["exposure_history"].append({
                            "event_id": f"EV_{event_seq:08d}",
                            "event_type": "exposure_delivered",
                            "agent_id": agent["agent_id"],
                            "status": "delivered", "sent_to_model": True,
                            "cards": delivered,
                        })
                        speech_seq += 1
                        state["routine_speech_history"].append({
                            "speech_id": f"SP_{speech_seq:08d}",
                            "event_id": f"RS_{speech_seq:08d}",
                            "world_id": world_id, "agent_id": agent["agent_id"],
                            "topic_id": topic_id, "phase": "exposure", "round": round_no,
                            "logical_seq": round_no * 100 + index + 1,
                            "event_type": "discussion_reply", "role": "assistant",
                            "status": "completed", "generated": True,
                            "text": _routine_text(round_no), "is_demo": True,
                        })
                for index, agent in enumerate(population):
                    # 立场/错误标记只从控制器排程表重算，受试可见的公共卡里没有这些字段。
                    stances = [card["stance"] for row in schedules[agent["agent_id"]]
                               for card in row]
                    errors = sum(not card["is_accurate"] for row in schedules[agent["agent_id"]]
                                 for card in row)
                    seen = Counter(card["card_id"] for row in schedules[agent["agent_id"]]
                                   for card in row)
                    em = metrics.exposure_metrics(stances, agent["initial_stance"])
                    manipulation.append({
                        "agent_id": agent["agent_id"], "environment": environment,
                        "delivered_cards": len(stances), "Hn": em["Hn"], "X": em["X"],
                        "error_ratio": errors / len(stances),
                        "max_repeat": max(seen.values()),
                    })

                # T1：第8轮末的测量上下文＋常规自然发言→mirror 严格契约。
                state["timepoint"] = "T1"
                state["round"] = EXPOSURE_ROUNDS
                state["logical_seq"] = 900
                t1_visible = _prepare_contexts(state, population, q,
                                               full_audit=(environment == "A"),
                                               counter=measurement_audit, timepoint="T1")
                for agent in population:
                    samples.append({
                        "topic_id": topic_id, "block_id": block_id, "world_id": world_id,
                        "agent_id": agent["agent_id"], "environment": environment, "arm": None,
                        "timepoint": "T1", "condition_tag": "D" if environment == "D" else f"E{environment}",
                        "visible_card_ids": list(t1_visible[agent["agent_id"]]),
                    })
                for agent in population:
                    view = measurement.extract_t1_routine_speech_view(
                        state, agent["agent_id"], topic_id=topic_id, max_entries=8,
                        allow_demo=True)
                    _need(view["natural_routine_speech"] is True
                          and view["mirror_contract_ready"] is True
                          and view["writes_to_memory"] is False
                          and view["world_state_before_sha256"] == view["world_state_after_sha256"],
                          "t1_routine_view_invalid")
                    measurement_audit["checks"] += 1
                    raw = _dump(_mirror_output(packet, _routine_text(1)))
                    parsed = parse_stage_output(raw, "mirror", packet, user_view=view["user_view"])
                    _need(parsed["citation_audit"]["schema_valid"] is True
                          and parsed["citation_audit"]["formal_eligible"] is False,
                          "mirror_contract_invalid")
                    mirror_checks.append({
                        "topic_id": topic_id, "block_id": block_id,
                        "environment": environment, "agent_id": agent["agent_id"],
                        "packet_id": packet["packet_id"], "user_view": view["user_view"],
                        "user_view_sha256": view["user_view_sha256"],
                        "selected_entry_count": view["selected_entry_count"],
                        "raw_text": raw,
                        "output_sha256": _sha(parsed["output"]),
                        "citation_audit": parsed["citation_audit"],
                    })

                env_states[environment] = state
                if environment == "D":
                    d_t1_visible = t1_visible
                world_records.append({
                    "topic_id": topic_id, "block_id": block_id, "environment": environment,
                    "world_id": world_id, "arm": None,
                    "base_population_network_sha256": base_hash,
                    "initial_state_core_sha256": initial_core_hash,
                    "exposure_delivered_events": len(state["exposure_history"]),
                    "routine_speech_entries": len(state["routine_speech_history"]),
                    "discussion_slots_used": EXPOSURE_ROUNDS,
                    "t0_t1_measurement_contexts": agents_per_world * 2,
                    "manipulation": manipulation,
                    "t1_snapshot_sha256": _sha(state),
                })
            worlds.extend(world_records)

            # ---------------------------------------------------------- 阶段3
            parent = env_states["D"]
            parent_t1_sha256 = _sha(parent)
            parent_core_sha256 = _sha(_normalized_state(parent))
            alloc_seed = block_seed + 777
            allocation = scheduling.allocate(list(ARMS), alloc_seed)
            children = []
            pre_hashes = {}
            for row in allocation:
                arm = row["arm"]
                child_world_id = f"W_{tag}_{block_id}_D_{arm}"
                child = runtime.branch_state(parent, child_world_id)
                child["arm"] = arm
                row["world_id"] = child_world_id
                _need(child["parent_world_id"] == parent["world_id"], "branch_parent_link_invalid")
                pre_hashes[arm] = _sha(_normalized_state(child))
                _need(pre_hashes[arm] == parent_core_sha256,
                      "branch_pre_treatment_state_hash_mismatch")
                children.append(child)
            _need(_sha(parent) == parent_t1_sha256, "branching_mutated_parent")
            identity_distinct = all(
                child["messages"] is not other["messages"]
                and child["agents"] is not other["agents"]
                and child["exposure_history"] is not other["exposure_history"]
                for index, child in enumerate(children)
                for other in children[index + 1:])
            _need(identity_distinct, "branch_shares_mutable_state")
            probe_agent = population[0]["agent_id"]
            children[0]["messages"][probe_agent].append(
                {"role": "user", "content": "__isolation_probe__"})
            others_clean = all(not child["messages"][probe_agent] for child in children[1:])
            parent_clean = not parent["messages"][probe_agent]
            children[0]["messages"][probe_agent].pop()
            _need(others_clean and parent_clean and _sha(parent) == parent_t1_sha256,
                  "isolation_probe_failed")
            _need(all(_sha(_normalized_state(child)) == parent_core_sha256 for child in children),
                  "isolation_probe_changed_state")

            for row, child in zip(allocation, children):
                arm = row["arm"]
                child_world_id = child["world_id"]
                delivered_rows = []
                for round_no in ROUNDS:
                    item = public["intervention_packets"][arm][str(round_no)]
                    visible = item["visible_text"]
                    for agent in population:
                        delivered_rows.append({
                            "round": round_no, "agent_id": agent["agent_id"],
                            "char_count": len(visible),
                            "visible_text_sha256": _bytes_sha(visible),
                            "response_opportunities": 1,
                        })
                        child["intervention_history"].append({
                            "event_type": "intervention_delivered", "arm": arm,
                            "round": round_no, "agent_id": agent["agent_id"],
                            "char_count": len(visible),
                            "visible_text_sha256": _bytes_sha(visible),
                            "sent_to_model": False,
                        })
                        intervention_delivery_count += 1
                # T2：两轮干预后（8锚题＋4迁移题全部由合成器输出）。
                child["timepoint"] = "T2"
                t2_visible = _prepare_contexts(child, population, q,
                                               counter=measurement_audit, timepoint="T2")
                for agent in population:
                    samples.append({
                        "topic_id": topic_id, "block_id": block_id, "world_id": child_world_id,
                        "agent_id": agent["agent_id"], "environment": "D", "arm": arm,
                        "timepoint": "T1", "condition_tag": "D",
                        "visible_card_ids": list(d_t1_visible[agent["agent_id"]]),
                    })
                    samples.append({
                        "topic_id": topic_id, "block_id": block_id, "world_id": child_world_id,
                        "agent_id": agent["agent_id"], "environment": "D", "arm": arm,
                        "timepoint": "T2", "condition_tag": f"A{arm}",
                        "visible_card_ids": list(t2_visible[agent["agent_id"]]),
                    })
                # 4轮保持：只用该议题独立的36张保持卡与保持讨论槽。
                retention_schedules = {}
                for index, agent in enumerate(population):
                    retention_schedules[agent["agent_id"]] = scheduling.schedule_cards(
                        retention_cards, agent["initial_stance"], "D", RETENTION_ROUNDS,
                        (block_seed + index + 3) % 6, max_repeats=2)
                for round_no in range(1, RETENTION_ROUNDS + 1):
                    slot = retention_slots[round_no]
                    _need(slot["frozen"] is True and slot["generated_live"] is False,
                          "retention_slot_not_frozen")
                    for agent in population:
                        batch = retention_schedules[agent["agent_id"]][round_no - 1]
                        _need(len(batch) == 6, "retention_round_card_count_invalid")
                        event_seq += 1
                        child["exposure_history"].append({
                            "event_id": f"EV_{event_seq:08d}",
                            "event_type": "exposure_delivered",
                            "agent_id": agent["agent_id"],
                            "status": "delivered", "sent_to_model": True,
                            "cards": [_demo_card(retention_public[card["card_id"]])
                                      for card in batch],
                        })
                        retention_delivery_count += 1
                # T3：主终点测量。
                child["timepoint"] = "T3"
                t3_visible = _prepare_contexts(child, population, q,
                                               full_audit=(arm == "I0"),
                                               counter=measurement_audit, timepoint="T3")
                for agent in population:
                    samples.append({
                        "topic_id": topic_id, "block_id": block_id, "world_id": child_world_id,
                        "agent_id": agent["agent_id"], "environment": "D", "arm": arm,
                        "timepoint": "T3", "condition_tag": f"A{arm}",
                        "visible_card_ids": list(t3_visible[agent["agent_id"]]),
                    })
                branches.append({
                    "topic_id": topic_id, "block_id": block_id, "arm": arm,
                    "slot": row["slot"], "response_seed": row["response_seed"],
                    "world_id": child_world_id, "parent_world_id": parent["world_id"],
                    "parent_t1_sha256": parent_t1_sha256,
                    "parent_t1_core_sha256": parent_core_sha256,
                    "branch_pre_treatment_core_sha256": pre_hashes[arm],
                    "intervention_deliveries": delivered_rows,
                    "retention_delivered_events": RETENTION_ROUNDS * agents_per_world,
                    "intervention_history": child["intervention_history"],
                    "t2_t3_measurement_contexts": agents_per_world * 2,
                })
            allocation_blocks.append({
                "topic_id": topic_id, "block_id": block_id, "seed": alloc_seed,
                "rows": [{"slot": row["slot"], "arm": row["arm"],
                          "response_seed": row["response_seed"],
                          "world_id": row["world_id"]} for row in allocation],
            })
            _need(_sha(parent) == parent_t1_sha256
                  and _sha(_normalized_state(parent)) == parent_core_sha256,
                  "branch_runtime_mutated_parent")

    allocations = {
        "contract": "allocation_registry_v1",
        "arms": list(ARMS),
        "seed": seed,
        "blocks": allocation_blocks,
    }

    # -------------------------------------------------------------- 阶段4
    entries = []
    synthetic_responses = []
    for spec in samples:
        topic_id = spec["topic_id"]
        raw = _dump(_synthetic_measurement(spec, questionnaires[topic_id]))
        record = measurement.parse_subject_measurement_output(
            raw, questionnaires[topic_id],
            visible_card_ids=spec["visible_card_ids"], timepoint=spec["timepoint"])
        _need(record["is_demo"] is True and record["formal_eligible"] is False,
              "synthetic_measurement_identity_invalid")
        entries.append({
            "parsed_measurement": record,
            "gold_bundle": gold_bundles[topic_id],
            "world_id": spec["world_id"], "agent_id": spec["agent_id"],
            "environment": spec["environment"], "arm": spec["arm"],
            "timepoint": spec["timepoint"], "block_id": spec["block_id"],
        })
        synthetic_responses.append({
            "topic_id": topic_id, "world_id": spec["world_id"],
            "agent_id": spec["agent_id"], "timepoint": spec["timepoint"],
            "rehearsal_mode": REHEARSAL_MODE,
            "synthetic_rehearsal_response": True, "is_demo": True,
            "actual_model_calls": 0, "human_reviews_completed": 0,
            "approved_for_experiment": False, "formal_eligible": False,
            "measurement_raw_sha256": _bytes_sha(raw),
        })
    registry = measurement.create_blind_sample_registry(entries, registry_seed=seed)
    _need(registry["is_demo"] is True and registry["new_model_calls"] == 0
          and registry["formal_eligible"] is False, "blind_registry_identity_invalid")
    _need(registry["entry_count"] == len(samples)
          and len(registry["public_blind_packets"]) == len(samples),
          "blind_registry_count_invalid")

    blind_evaluations = []
    for blind_packet in registry["public_blind_packets"]:
        sid = blind_packet["blind_sample_id"]
        bound = registry["private_mapping"][sid]
        source_index = bound["source_entry_index"]
        spec = samples[source_index]
        blind_raw = _dump(_synthetic_blind_evaluation(blind_packet, spec))
        evaluation = measurement.parse_blind_evaluation_output(blind_raw, blind_packet)
        blind_evaluations.append(evaluation)
        synthetic_responses[source_index]["blind_evaluation_raw_sha256"] = _bytes_sha(blind_raw)

    mapping = registry["private_mapping"]
    measurements_by_id = {
        sid: deepcopy(registry["source_entries"][bound["source_entry_index"]]["parsed_measurement"])
        for sid, bound in mapping.items()
    }
    gold_by_id = {
        sid: gold_bundles[samples[bound["source_entry_index"]]["topic_id"]]
        for sid, bound in mapping.items()
    }
    reconciled = measurement.reconcile_blind_evaluations(
        registry, blind_evaluations, measurements_by_id, gold_by_id,
        expected_registry_sha256=registry["registry_sha256"])
    _need(reconciled["complete_panel_records"] == reconciled["expected_samples"] == len(samples)
          and reconciled["pending_blind_evaluations"] == 0
          and reconciled["status"] == "all_blind_evaluations_reconciled"
          and reconciled["writes_to_memory"] is False
          and reconciled["is_demo"] is True, "blind_panel_not_fully_reconciled")

    aggregation = analysis.aggregate_panel_to_networks(
        reconciled, expected_agents_per_world=agents_per_world, allow_demo=True)
    _need(aggregation["is_demo"] is True and aggregation["formal_eligible"] is False,
          "network_aggregation_identity_invalid")
    _need(all(row["missing_agents"] == 0 and row["valid"] is True and row["blind_valid"] is True
              for row in aggregation["network_records"]), "network_aggregation_incomplete")
    exposure = analysis.analyze_exposure_2x2(
        aggregation, target_topics=list(TOPIC_ORDER), metric="BS", allow_demo=True)
    _need(exposure["overall"]["estimable"] is True and not exposure["missing_topics"],
          "exposure_2x2_not_estimable")
    analysis_seed = seed + 1
    intervention = {}
    for endpoint in ("T2", "T3"):
        outcome = analysis.analyze_intervention_effects(
            aggregation, target_topics=list(TOPIC_ORDER), allocations=allocations,
            endpoint_timepoint=endpoint, metric="BS", bootstrap_reps=bootstrap_reps,
            permutation_reps=permutation_reps, seed=analysis_seed, allow_demo=True)
        _need(all(value is not None for value in outcome["p_raw"].values())
              and all(value is not None for value in outcome["p_adjusted"].values())
              and outcome["randomization"]["holm_operator"] == "metrics.holm",
              "randomization_pvalues_missing")
        _need(outcome["allocation_validation"]["complete_blocks"]
              == outcome["allocation_validation"]["registered_blocks"],
              "allocation_incomplete_blocks")
        intervention[endpoint] = outcome

    # 操纵检查：A/B 均衡、C/D 同质；A/C 零错误、B/D 每轮两张错误卡。
    by_env = {env: [row for world in worlds for row in world["manipulation"]
                    if row["environment"] == env] for env in ENVIRONMENTS}
    manipulation_summary = {}
    for env in ENVIRONMENTS:
        rows = by_env[env]
        _need(bool(rows), "manipulation_rows_missing")
        mean_x = sum(row["X"] for row in rows) / len(rows)
        mean_hn = sum(row["Hn"] for row in rows) / len(rows)
        mean_error = sum(row["error_ratio"] for row in rows) / len(rows)
        manipulation_summary[env] = {"samples": len(rows), "mean_X": mean_x,
                                     "mean_Hn": mean_hn, "mean_error_ratio": mean_error,
                                     "max_repeat": max(row["max_repeat"] for row in rows)}
    _need(abs(manipulation_summary["A"]["mean_X"] - 100 / 3) < 1e-9
          and abs(manipulation_summary["B"]["mean_X"] - 100 / 3) < 1e-9
          and abs(manipulation_summary["C"]["mean_X"] - 100 / 6) < 1e-9
          and abs(manipulation_summary["D"]["mean_X"] - 100 / 6) < 1e-9,
          "balanced_vs_homogeneous_exposure_not_separated")
    _need(manipulation_summary["A"]["mean_Hn"] > manipulation_summary["C"]["mean_Hn"],
          "entropy_manipulation_not_separated")
    _need(manipulation_summary["A"]["mean_error_ratio"] == 0
          and manipulation_summary["C"]["mean_error_ratio"] == 0
          and manipulation_summary["B"]["mean_error_ratio"] > 0
          and manipulation_summary["D"]["mean_error_ratio"] > 0,
          "error_card_manipulation_not_separated")

    stage2 = {
        "world_count": len(worlds),
        "worlds": worlds,
        "mirror_checks": mirror_checks,
        "mirror_check_count": len(mirror_checks),
        "t0_t1_contexts": len(worlds) * agents_per_world * 2,
        "manipulation_summary": manipulation_summary,
        "exposure_delivered_events": sum(w["exposure_delivered_events"] for w in worlds),
        "routine_speech_entries": sum(w["routine_speech_entries"] for w in worlds),
    }
    stage3 = {
        "branch_count": len(branches),
        "branches": branches,
        "intervention_delivery_count": intervention_delivery_count,
        "retention_delivery_count": retention_delivery_count,
        "allocation_blocks": len(allocation_blocks),
        "identity_distinct": identity_distinct,
        "isolation_probe_clean": others_clean and parent_clean,
        "all_parent_hashes_unchanged": True,
    }
    stage4 = {
        "rehearsal_mode": REHEARSAL_MODE,
        "human_reviews_completed": 0,
        "approved_for_experiment": False,
        "formal_eligible": False,
        "expected_samples": len(samples),
        "synthetic_response_manifest_sha256": _sha(synthetic_responses),
        "synthetic_response_count": len(synthetic_responses),
        "all_synthetic_rehearsal_responses": all(
            row["synthetic_rehearsal_response"] is True and row["is_demo"] is True
            for row in synthetic_responses),
        "registry_sha256": registry["registry_sha256"],
        "registry_entry_count": registry["entry_count"],
        "source_entries_sha256": registry["source_entries_sha256"],
        "public_packets_sha256": registry["public_packets_sha256"],
        "private_mapping_sha256": registry["private_mapping_sha256"],
        "blind_packet_sendable_field": registry["sendable_field"],
        "blind_packet_leak_scan_count": len(registry["public_blind_packets"]),
        "blind_packet_leaks_found": 0,
        "complete_blind_evaluations": len(blind_evaluations),
        "pending_blind_evaluations": reconciled["pending_blind_evaluations"],
        "complete_panel_records": reconciled["complete_panel_records"],
        "network_record_count": len(aggregation["network_records"]),
        "all_networks_valid": all(row["missing_agents"] == 0 and row["valid"] and row["blind_valid"]
                                  for row in aggregation["network_records"]),
        "missing_agents_total": sum(row["missing_agents"] for row in aggregation["network_records"]),
        "measurement_zero_writeback_checks": measurement_audit["checks"],
        "full_state_exclusion_audits": measurement_audit["full_state_audits"],
        "measurement_context_method":
            "per_subject_read_only_measurement_view_all_agents_all_timepoints_plus_sampled_full_state_exclusion_audits",
        "measurement_zero_writeback_all_passed": True,
    }
    analysis_block = {
        "reconciled_panel": reconciled,
        "network_aggregation": aggregation,
        "exposure_2x2": exposure,
        "intervention": intervention,
        "allocations": allocations,
        "rehearsal_mode": REHEARSAL_MODE,
        "statistical_outputs_are_synthetic": True,
        "is_demo": True,
        "actual_model_calls": 0,
        "human_reviews_completed": 0,
        "approved_for_experiment": False,
        "formal_eligible": False,
        "note": "p_raw/p_adjusted/效应值全部由确定性合成回答产生，只验证统计链路可运行，不是效应结论",
    }
    result = {
        "contract": REHEARSAL_VERSION,
        "rehearsal_mode": REHEARSAL_MODE,
        "notice": NOTICE,
        "protocol_sections": list(PROTOCOL_SECTIONS),
        **deepcopy(_HONEST_GATES),
        "synthetic_response_generator": "deterministic_hash_of_visible_card_ids_and_preset_condition_tag_v1",
        "gold_never_read_by_response_generator": True,
        "parameters": {
            "blocks_per_topic": blocks_per_topic, "agents_per_world": agents_per_world,
            "seed": seed, "bootstrap_reps": bootstrap_reps,
            "permutation_reps": permutation_reps, "analysis_seed": analysis_seed,
            "topics": list(TOPIC_ORDER), "environments": list(ENVIRONMENTS),
            "arms": list(ARMS), "exposure_rounds": EXPOSURE_ROUNDS,
            "intervention_rounds": len(ROUNDS), "retention_rounds": RETENTION_ROUNDS,
            "timepoints": ["T0", "T1", "T2", "T3"],
        },
        "stage1_materials": {
            "contract": pm.ALL_TOPICS_VERSION,
            "evidence_root": str(Path(evidence_root).resolve()),
            "pilot_manifest_build_id": materials_bundle["pilot_manifest"]["build_id"],
            "topic_count": len(TOPIC_ORDER),
            "topics": topic_materials,
            "totals": {
                "exposure_cards": sum(t["exposure_cards"] for t in topic_materials.values()),
                "retention_cards": sum(t["retention_cards"] for t in topic_materials.values()),
                "discussion_slots": sum(t["discussion_slots"] for t in topic_materials.values()),
                "intervention_packets": sum(t["intervention_packets"] for t in topic_materials.values()),
                "questionnaires": len(questionnaires),
                "private_gold_bundles": len(gold_bundles),
            },
            "frozen_packets": frozen_packets,
            "demo_questionnaires": questionnaires,
            "demo_gold_bundle_sha256": {tid: gold_bundles[tid]["bundle_sha256"]
                                        for tid in TOPIC_ORDER},
        },
        "stage2_exposure": stage2,
        "stage3_branches": stage3,
        "stage4_measurement": stage4,
        "analysis": analysis_block,
        "acceptance_gates": _acceptance_gates(topic_materials, stage2, stage3, stage4, analysis_block),
    }
    result["integrity_sha256"] = _sha({k: v for k, v in result.items() if k != "integrity_sha256"})
    return result


def _acceptance_gates(materials, stage2, stage3, stage4, analysis_block):
    """§15.1 门槛的只读快照，verify 会逐项重新独立核验。"""
    return {
        "section": "15.1",
        "three_zone_zero_gold_leak": all(t["zone_leak_scan"]["leak_free"] for t in materials.values()),
        "six_arms_nonempty_600_to_800": all(
            row["nonempty"] and row["in_range"] and row["extractive_control_constraint_satisfied"]
            for t in materials.values() for row in t["dose_rows"]),
        "all_delivered_exposure_events": stage2["exposure_delivered_events"] > 0,
        "parent_child_pre_treatment_hash_match": all(
            b["branch_pre_treatment_core_sha256"] == b["parent_t1_core_sha256"]
            for b in stage3["branches"]),
        "zero_cross_arm_mutable_sharing": (stage3["identity_distinct"]
                                           and stage3["isolation_probe_clean"]
                                           and stage3["all_parent_hashes_unchanged"]),
        "measurement_zero_memory_writeback": stage4["measurement_zero_writeback_all_passed"],
        "full_panel_reconciled": (stage4["complete_panel_records"] == stage4["expected_samples"]
                                  and stage4["pending_blind_evaluations"] == 0),
        "network_aggregation_valid": (stage4["all_networks_valid"]
                                      and stage4["missing_agents_total"] == 0),
        "honest_gates_locked": True,
        "formal_launch_remains_closed": True,
    }


def verify_pilot_system_rehearsal(rehearsal_result) -> dict:
    """只凭结果对象独立复验 §15.1 门槛与诚实门禁；任何篡改都必须被拒绝。"""
    _need(type(rehearsal_result) is dict, "result_invalid")
    _need(rehearsal_result.get("contract") == REHEARSAL_VERSION, "contract_invalid")
    body = {key: value for key, value in rehearsal_result.items() if key != "integrity_sha256"}
    _need(rehearsal_result.get("integrity_sha256") == _sha(body), "integrity_hash_mismatch")

    # 1. 诚实门禁位（逐项，不依赖 acceptance_gates 的自报布尔值）。
    for key, expected in _HONEST_GATES.items():
        _need(rehearsal_result.get(key) == expected, "honest_gate_" + key)
    _need(rehearsal_result.get("subject_delivered") is False
          and rehearsal_result.get("statistical_outputs_are_synthetic") is True,
          "honest_gate_delivery_or_statistics")
    _need(rehearsal_result["analysis"].get("statistical_outputs_are_synthetic") is True
          and rehearsal_result["analysis"].get("rehearsal_mode") == REHEARSAL_MODE
          and rehearsal_result["analysis"].get("human_reviews_completed") == 0
          and rehearsal_result["analysis"].get("approved_for_experiment") is False
          and rehearsal_result["analysis"].get("formal_eligible") is False,
          "analysis_synthetic_marker_missing")
    _need(rehearsal_result["stage4_measurement"]["all_synthetic_rehearsal_responses"] is True
          and rehearsal_result["stage4_measurement"].get("human_reviews_completed") == 0
          and rehearsal_result["stage4_measurement"].get("approved_for_experiment") is False
          and rehearsal_result["stage4_measurement"].get("formal_eligible") is False,
          "synthetic_response_marker_missing")

    params = rehearsal_result["parameters"]
    materials = rehearsal_result["stage1_materials"]
    stage2 = rehearsal_result["stage2_exposure"]
    stage3 = rehearsal_result["stage3_branches"]
    stage4 = rehearsal_result["stage4_measurement"]
    block = rehearsal_result["analysis"]

    # 2. 阶段1：三议题三区零泄漏 + 六臂两轮全部非空且600–800。
    _need(tuple(materials["demo_questionnaires"]) == TOPIC_ORDER
          and materials["topic_count"] == 3, "topic_set_invalid")
    _need(materials["totals"] == {
        "exposure_cards": 216, "retention_cards": 108, "discussion_slots": 36,
        "intervention_packets": 36, "questionnaires": 3, "private_gold_bundles": 3,
    }, "material_totals_invalid")
    dose_rows = []
    for topic_id in TOPIC_ORDER:
        topic = materials["topics"][topic_id]
        _need(topic["material_verification_passed"] is True, "topic_verification_flag_invalid")
        scan = topic["zone_leak_scan"]
        _need(scan["leak_free"] is True and scan["forbidden_keys_found"] == []
              and scan["gold_text_leak"] is False
              and scan["three_zone_hashes_distinct"] is True
              and scan["private_zone_only"] is True, "three_zone_gold_leak")
        _need(len(topic["dose_rows"]) == 12, "dose_row_count_invalid")
        for row in topic["dose_rows"]:
            _need(row["nonempty"] is True and row["in_range"] is True
                  and pm.DOSE_MIN_CHARS <= row["char_count"] <= pm.DOSE_MAX_CHARS
                  and type(row["visible_text_sha256"]) is str and len(row["visible_text_sha256"]) == 64
                  and row["response_opportunities"] == 1
                  and row["extractive_control_constraint_satisfied"] is True,
                  "six_arm_dose_invalid")
            dose_rows.append(row)
        _need({(row["arm"], row["round"]) for row in topic["dose_rows"]}
              == {(arm, r) for arm in ARMS for r in ROUNDS}, "six_arm_coverage_invalid")
        _need(len(materials["demo_questionnaires"][topic_id]["anchor_items"]) == 8
              and len(materials["demo_questionnaires"][topic_id]["transfer_items"]) == 4,
              "questionnaire_item_count_invalid")
    _need(len(dose_rows) == 36, "dose_total_invalid")

    # 3. 阶段2：四环境共享基础哈希、操纵检查、T1常规发言→mirror 严格重放。
    expected_worlds = 3 * params["blocks_per_topic"] * len(ENVIRONMENTS)
    _need(len(stage2["worlds"]) == expected_worlds == stage2["world_count"],
          "exposure_world_count_invalid")
    for world in stage2["worlds"]:
        _need(world["arm"] is None and world["environment"] in ENVIRONMENTS,
              "exposure_world_condition_invalid")
        _need(world["exposure_delivered_events"]
              == EXPOSURE_ROUNDS * params["agents_per_world"], "exposure_event_count_invalid")
        _need(world["routine_speech_entries"]
              == EXPOSURE_ROUNDS * params["agents_per_world"], "routine_speech_count_invalid")
        _need(world["t0_t1_measurement_contexts"] == params["agents_per_world"] * 2,
              "measurement_context_count_invalid")
        _need(type(world["initial_state_core_sha256"]) is str
              and len(world["initial_state_core_sha256"]) == 64
              and type(world["base_population_network_sha256"]) is str
              and len(world["base_population_network_sha256"]) == 64
              and len(world["t1_snapshot_sha256"]) == 64,
              "world_hash_invalid")
    for topic_id in TOPIC_ORDER:
        for block_index in range(params["blocks_per_topic"]):
            block_id = f"B{block_index + 1:02d}"
            group = [w for w in stage2["worlds"]
                     if w["topic_id"] == topic_id and w["block_id"] == block_id]
            _need(len(group) == 4
                  and {w["environment"] for w in group} == set(ENVIRONMENTS)
                  and len({w["base_population_network_sha256"] for w in group}) == 1
                  and len({w["initial_state_core_sha256"] for w in group}) == 1,
                  "four_environments_not_shared_base_state")
    manipulation = stage2["manipulation_summary"]
    _need(abs(manipulation["A"]["mean_X"] - 100 / 3) < 1e-9
          and abs(manipulation["B"]["mean_X"] - 100 / 3) < 1e-9
          and abs(manipulation["C"]["mean_X"] - 100 / 6) < 1e-9
          and abs(manipulation["D"]["mean_X"] - 100 / 6) < 1e-9
          and manipulation["A"]["mean_Hn"] > manipulation["C"]["mean_Hn"]
          and manipulation["A"]["mean_error_ratio"] == 0
          and manipulation["C"]["mean_error_ratio"] == 0
          and manipulation["B"]["mean_error_ratio"] > 0
          and manipulation["D"]["mean_error_ratio"] > 0,
          "manipulation_check_invalid")
    _need(stage2["mirror_check_count"] == expected_worlds * params["agents_per_world"]
          == len(stage2["mirror_checks"]), "mirror_check_count_invalid")
    for check in stage2["mirror_checks"]:
        packet = materials["frozen_packets"][check["topic_id"]]
        _need(check["packet_id"] == packet["packet_id"], "mirror_packet_binding_invalid")
        _need(check["user_view_sha256"] == _bytes_sha(check["user_view"]),
              "mirror_user_view_hash_invalid")
        _need(check["selected_entry_count"] == 8, "mirror_routine_entry_count_invalid")
        parsed = parse_stage_output(check["raw_text"], "mirror", packet,
                                    user_view=check["user_view"])
        _need(_sha(parsed["output"]) == check["output_sha256"]
              and parsed["citation_audit"] == check["citation_audit"]
              and parsed["citation_audit"]["schema_valid"] is True,
              "mirror_contract_replay_failed")

    # 4. 阶段3：父子处理前哈希一致、六臂全量投递、零可变状态共享。
    expected_branches = 3 * params["blocks_per_topic"] * len(ARMS)
    _need(len(stage3["branches"]) == expected_branches == stage3["branch_count"],
          "branch_count_invalid")
    for branch in stage3["branches"]:
        _need(branch["arm"] in ARMS and branch["slot"] in range(6)
              and type(branch["response_seed"]) is int, "branch_allocation_invalid")
        _need(branch["branch_pre_treatment_core_sha256"] == branch["parent_t1_core_sha256"]
              and len(branch["parent_t1_sha256"]) == 64,
              "parent_child_pre_treatment_hash_mismatch")
        _need(len(branch["intervention_deliveries"]) == len(ROUNDS) * params["agents_per_world"],
              "intervention_delivery_count_invalid")
        _need(len(branch["intervention_history"]) == len(branch["intervention_deliveries"]),
              "intervention_history_count_invalid")
        for row in branch["intervention_deliveries"]:
            _need(pm.DOSE_MIN_CHARS <= row["char_count"] <= pm.DOSE_MAX_CHARS
                  and row["response_opportunities"] == 1
                  and len(row["visible_text_sha256"]) == 64,
                  "intervention_delivery_dose_invalid")
        _need(branch["retention_delivered_events"]
              == RETENTION_ROUNDS * params["agents_per_world"], "retention_delivery_count_invalid")
        _need(branch["t2_t3_measurement_contexts"] == params["agents_per_world"] * 2,
              "t2_t3_context_count_invalid")
    _need(stage3["identity_distinct"] is True and stage3["isolation_probe_clean"] is True
          and stage3["all_parent_hashes_unchanged"] is True,
          "cross_arm_isolation_invalid")
    _need(stage3["intervention_delivery_count"]
          == expected_branches * len(ROUNDS) * params["agents_per_world"],
          "intervention_delivery_total_invalid")
    _need(stage3["retention_delivery_count"]
          == expected_branches * RETENTION_ROUNDS * params["agents_per_world"],
          "retention_delivery_total_invalid")

    # 5. 阶段4：全量面板回填、测量零写回、网络聚合重算 + 统计数值复现。
    expected_samples = 3 * params["blocks_per_topic"] * params["agents_per_world"] * (
        len(ENVIRONMENTS) * 2 + len(ARMS) * 3)
    _need(stage4["expected_samples"] == expected_samples
          and stage4["complete_panel_records"] == expected_samples
          and stage4["synthetic_response_count"] == expected_samples
          and stage4["pending_blind_evaluations"] == 0
          and stage4["complete_blind_evaluations"] == expected_samples
          and stage4["blind_packet_leak_scan_count"] == expected_samples
          and stage4["blind_packet_leaks_found"] == 0
          and stage4["blind_packet_sendable_field"] == "public_blind_packets",
          "blind_panel_coverage_invalid")
    _need(stage4["full_state_exclusion_audits"] == 3 * params["blocks_per_topic"] * 2,
          "full_state_exclusion_audit_count_invalid")
    _need(stage4["measurement_zero_writeback_checks"]
          == (stage2["world_count"] + stage3["branch_count"]) * 2 * params["agents_per_world"]
          + stage2["world_count"] * params["agents_per_world"]
          and stage4["measurement_zero_writeback_all_passed"] is True,
          "measurement_zero_writeback_invalid")
    _need(stage4["all_synthetic_rehearsal_responses"] is True,
          "synthetic_response_flag_invalid")
    _need(stage4["all_networks_valid"] is True and stage4["missing_agents_total"] == 0,
          "network_aggregation_validity_invalid")
    panel = block["reconciled_panel"]
    _need(panel["contract"] == "reconciled_blind_panel_v1"
          and panel["is_demo"] is True and panel["formal_eligible"] is False
          and panel["writes_to_memory"] is False
          and panel["trusted_registry_digest_supplied"] is True
          and panel["registry_sha256"] == stage4["registry_sha256"],
          "reconciled_panel_identity_invalid")
    _need(_sha(panel["panel_records"]) == panel["panel_sha256"]
          and len(panel["panel_records"]) == expected_samples,
          "panel_hash_or_count_invalid")
    aggregation = block["network_aggregation"]
    _need(aggregation["is_demo"] is True and aggregation["formal_eligible"] is False
          and _sha(aggregation["network_records"]) == aggregation["network_records_sha256"]
          and len(aggregation["network_records"]) == stage4["network_record_count"],
          "network_aggregation_hash_invalid")
    rebuilt = analysis.aggregate_panel_to_networks(
        panel, expected_agents_per_world=params["agents_per_world"], allow_demo=True)
    _need(_sha(rebuilt) == _sha(aggregation), "network_aggregation_not_reproducible")
    rebuilt_exposure = analysis.analyze_exposure_2x2(
        aggregation, target_topics=list(TOPIC_ORDER), metric="BS", allow_demo=True)
    _need(_sha(rebuilt_exposure) == _sha(block["exposure_2x2"]),
          "exposure_2x2_not_reproducible")
    _need(block["exposure_2x2"]["overall"]["estimable"] is True,
          "exposure_2x2_not_estimable")
    for endpoint in ("T2", "T3"):
        outcome = block["intervention"][endpoint]
        rebuilt_outcome = analysis.analyze_intervention_effects(
            aggregation, target_topics=list(TOPIC_ORDER),
            allocations=block["allocations"], endpoint_timepoint=endpoint, metric="BS",
            bootstrap_reps=params["bootstrap_reps"],
            permutation_reps=params["permutation_reps"],
            seed=params["analysis_seed"], allow_demo=True)
        _need(_sha(rebuilt_outcome) == _sha(outcome),
              "intervention_statistics_not_reproducible")
        _need(all(value is not None for value in outcome["p_raw"].values())
              and all(value is not None for value in outcome["p_adjusted"].values())
              and outcome["randomization"]["holm_operator"] == "metrics.holm"
              and outcome["randomization"]["method"]
              == "within_block_six_arm_slot_label_permutation",
              "randomization_results_invalid")
        _need(outcome["allocation_validation"]["complete_blocks"]
              == outcome["allocation_validation"]["registered_blocks"]
              == len(block["allocations"]["blocks"]),
              "allocation_validation_block_count_invalid")

    # 6. acceptance_gates 汇总与重新计算的值一致。
    expected_gates = _acceptance_gates(materials["topics"], stage2, stage3, stage4, block)
    _need(rehearsal_result["acceptance_gates"] == expected_gates,
          "acceptance_gate_summary_mismatch")
    _need(all(value is True for value in expected_gates.values() if type(value) is bool),
          "acceptance_gate_not_all_passed")

    return {
        "contract": VERIFY_VERSION,
        "passed": True,
        "rehearsal_mode": REHEARSAL_MODE,
        "section": "15.1",
        "topic_count": materials["topic_count"],
        "world_count": stage2["world_count"],
        "mirror_contract_replays": len(stage2["mirror_checks"]),
        "branch_count": stage3["branch_count"],
        "six_arm_dose_packets": len(dose_rows),
        "samples_reconciled": stage4["complete_panel_records"],
        "network_records": len(aggregation["network_records"]),
        "statistical_endpoints": ["T2", "T3"],
        "checks": {
            "integrity_hash_verified": True,
            "honest_gates_locked": True,
            "three_zone_zero_gold_leak": True,
            "six_arms_nonempty_600_to_800": True,
            "four_environments_shared_base_state": True,
            "t1_routine_speech_to_mirror_replayed": True,
            "parent_child_pre_treatment_hash_match": True,
            "zero_cross_arm_mutable_sharing": True,
            "intervention_all_six_arms_delivered": True,
            "measurement_zero_memory_writeback": True,
            "full_panel_reconciled_and_network_valid": True,
            "network_2x2_and_randomization_holm_reproducible": True,
        },
        "rehearsal_is_not_real_experiment": True,
        "is_demo": True,
        "actual_model_calls": 0,
        "human_reviews_completed": 0,
        "formal_eligible": False,
        "approved_for_experiment": False,
        "new_cost_cny": "0",
    }
