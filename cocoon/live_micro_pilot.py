"""步骤40：TOPIC_01 真实 mimo 端到端微型实证预演（n=2 个体 × 3 臂）。

链路：T0 → 环境 D 8 轮曝光 → T1 常规发言 + 真实 mirror → T1 测量（父快照）
→ I0/I5/I4 三臂平行分叉（2 轮干预 + 单次回应）→ T2 → 4 轮保持 → T3
→ 匿名注册表 + 独立盲评 → reconcile 面板。

边界（不可放宽）：
- 只有 `execute=True` 且凭据/账本/配置就绪才发请求，否则 0 调用返回。
- 所有尝试（含被校验器拒绝者）写入 EventStore 与共享账本；本地不修剪、不改写模型文本。
- MiMoTransport 只接受 1 条 system + 1 条 user：受试历史按 `_flatten` 确定性展开为
  转录文本，规则随清单记录，可离线重建。
- 微型样本只作机制与端到端演示，不作统计推断；formal_eligible 恒为 False。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from . import measurement, pilot_materials as pm, scheduling
from .assistant import AssistantAdapter, _dumps, _response_metadata, effective_system_prompt
from .budget import BudgetError
from .contracts import _plain_text, parse_stage_output, validate_packet
from .mimo import MiMoError, MiMoTransport
from .mimo_probe import _ledger, mimo_status
from .prompts import load_prompt_bundle
from .runtime import (EventStore, _parse_json, _verify_blob, canonical_hash, utc_now,
                      verify_events, write_json_new)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "1.0"
CONTRACT = "topic01_live_micro_pilot_v1"
RUNS_RELATIVE = "experiment/live_micro_pilot"
MANIFEST_RELATIVE = "experiment/review/live_micro_pilot_20261111/topic01_live_micro_pilot.json"
LEDGER_RELATIVE = "experiment/billing/mimo_initial_probe_v1.jsonl"
EVIDENCE_BUILD_RELATIVE = "experiment/evidence_candidates/v1_20261108/build_20261108T074108_f38ec5"
TOPIC_ID = "TOPIC_01"
BLOCK_ID = "B02"
BASE_SEED = 20261109
BLOCK_SEED = BASE_SEED + 101          # 与 system_rehearsal 的 TOPIC_01/B02 区组种子一致
ENVIRONMENT = "D"
SUBJECTS = ("S01", "S05")
ARMS = ("I0", "I5", "I4")
EXPOSURE_ROUNDS, RETENTION_ROUNDS, INTERVENTION_ROUNDS = 8, 4, (1, 2)
REGISTRY_SEED = 20261113
MAX_ATTEMPTS = 3
SPEECH_RANGE = (120, 280)
RESPONSE_RANGE = (20, 300)
PARENT_WORLD = "W_LIVE_T01_B02_D"
FLATTEN_RULE = ("system_messages_joined_blank_line__history_as_tagged_transcript__"
                "history_newlines_rendered_as_pipe__last_user_as_task_v1")
MEASURE_GUIDE = (
    "输出要求补充：只输出一个JSON对象，不要代码围栏、前后说明或Markdown。answers必须覆盖问卷全部题目"
    "（锚题与迁移题都要作答），每题p_true为0到1之间的数；brief_reason用一两句中文；cited_card_ids只能"
    "从visible_card_ids中选取，visible_card_ids为空时必须是[]。evidence_notes写1到3条，opposing_view三项"
    "各写一两句中文。evidence_notes与opposing_view的文字中不要出现任何英文字母、编号代码或卡片编号，"
    "也不要提及组别、时点、实验或测量本身；卡片编号只能出现在cited_card_ids数组里。")
MEASURE_FORMAT_NOTE = (
    "这一步是填写问卷，不是发帖或回应材料。只输出一个以“{”开头、以“}”结尾的JSON对象，字段为"
    "answers、evidence_notes、opposing_view；不要复述上面的经历记录，不要写任何JSON之外的文字。")
SPEECH_TASK = (
    "现在请你像平时在讨论区发帖一样，就这个议题写一段中文看法，说说你目前怎么想、依据是什么、还有什么"
    "拿不准。篇幅约{target}字（含标点按字计，硬性范围{low}到{high}字，超过{high}字会被退回，宁短勿长，"
    "写三到四句即可）。只输出这段话本身：一段连续纯文本，不要换行、标题、列表、引号、Markdown，"
    "全文不得出现任何英文字母（包括单个字母、缩写或符号名），不要提到卡片编号、实验、问卷、测量或打分。")
SPEECH_TARGET = 190
RESPONSE_GUIDE = (
    "请用约{target}字（硬性范围{low}到{high}字，宁短勿长）的一段中文纯文本简短回应上面这段阅读材料，"
    "说出你的想法或疑问。不要换行或Markdown，全文不得出现任何英文字母或编号，不要提到实验、问卷或测量。")
RESPONSE_TARGET = 120
PROMPT_REVISION = "micro_pilot_prompts_v3_target_chars_actionable_feedback_measure_format_note"
# 步骤41：只增强重试反馈（mirror 逐条引句诊断），主提示词与冻结校验器不变；TOPIC_01 清单不受影响。
FEEDBACK_REVISION = "micro_pilot_feedback_v4_mirror_quote_diagnostics"
BLIND_SYSTEM = (
    "你是独立盲评员。你只会看到一份匿名作答中的证据说明（evidence_notes）和对另一观点的复述"
    "（opposing_view），以及六个评估维度说明与异见理解量规；你不知道作答者身份、条件或时间，也不要"
    "猜测。请只输出一个严格JSON对象，字段仅为blind_sample_id、dimensions、opposing_scores，不要代码"
    "围栏或说明文字。blind_sample_id逐字照抄输入。dimensions必须恰好6项且顺序与输入一致，每项字段仅为"
    "dimension_id（逐字照抄）、mastered（布尔）、recognized_gap（布尔）、evidence_quote。mastered表示作答"
    "已准确掌握该维度要点；recognized_gap表示未掌握但明确意识到这是待查缺口。若mastered或recognized_gap"
    "任一为true，evidence_quote必须是从某一条evidence_notes的known_evidence或remaining_question中逐字"
    "摘取的连续片段（不得改字、不得取自opposing_view）；两者都为false时evidence_quote为null。"
    "opposing_scores字段仅为summary、main_evidence、boundary（各为整数0、1或2，按量规评分）与rationale"
    "（一两句中文理由，不要出现英文字母或编号）。")
CONDITION_LABELS = {"I0": "中性对照（原句摘录）", "I5": "等证据原句摘录对照", "I4": "破茧助手组织反思"}
TOPIC_IDS = ("TOPIC_01", "TOPIC_02", "TOPIC_03")
# 以冻结证据包为准：TOPIC_02=能源转型、TOPIC_03=天气信息核查（步骤34 起的三议题）。
TOPIC_LABELS = {"TOPIC_01": "气候与碳排放", "TOPIC_02": "能源转型", "TOPIC_03": "天气信息核查"}
SUBJECT_RULE = "S01_plus_first_opposite_stance_agent_scanning_from_S05_in_B02"
MATRIX_RELATIVE = "experiment/review/live_micro_pilot_20261111/three_topic_live_micro_pilot_matrix.json"
MATRIX_CONTRACT = "three_topic_live_micro_pilot_matrix_v1"


def topic_spec(topic_id=TOPIC_ID) -> dict:
    """议题规格：B02 区组种子与 system_rehearsal 对齐；受试按统一规则选出对立双方。

    规则：保留 S01，搭档为从 S05 起向后第一个与 S01 立场相反者（TOPIC_01 得 S05，原样复现步骤40）。
    """
    _need(topic_id in TOPIC_IDS, "topic_id_invalid")
    index = TOPIC_IDS.index(topic_id)
    number = f"{index + 1:02d}"
    block_seed = BASE_SEED + index * 10007 + 101
    population = scheduling.make_population(8, block_seed)
    stances = {agent["agent_id"]: agent["initial_stance"] for agent in population}
    order = [agent["agent_id"] for agent in population]
    partner = next(agent for agent in order[order.index("S05"):] if stances[agent] == -stances["S01"])
    return {"topic_id": topic_id, "topic_index": index, "number": number,
            "label": TOPIC_LABELS[topic_id], "block_id": BLOCK_ID, "block_seed": block_seed,
            "subjects": ("S01", partner), "stances": {"S01": stances["S01"], partner: stances[partner]},
            "parent_world": f"W_LIVE_T{number}_B02_D",
            "contract": f"topic{number}_live_micro_pilot_v1",
            "manifest_relative": f"experiment/review/live_micro_pilot_20261111/topic{number}_live_micro_pilot.json",
            "run_prefix": f"live_micro_pilot_{topic_id}_", "subject_rule": SUBJECT_RULE}


def _need(condition, code):
    if not condition:
        raise ValueError("live_micro_pilot:" + code)


def _sha_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 材料
def load_materials(evidence_build_dir=None, topic_id=TOPIC_ID) -> dict:
    """只读加载指定议题三区材料、问卷、金标准、冻结 full 证据包与排程（默认 TOPIC_01）。"""
    spec = topic_spec(topic_id)
    build = Path(evidence_build_dir or ROOT / EVIDENCE_BUILD_RELATIVE).resolve(strict=True)
    bundle = pm.build_all_topics_pilot_materials(build)
    zones = bundle["topics"][topic_id]["zones"]
    gold_zone = zones["zone_3_evaluator_private_gold"]
    questionnaire = measurement.validate_public_questionnaire(gold_zone["public_questionnaire"])
    gold = measurement.validate_private_gold_bundle(gold_zone["private_gold_bundle"], questionnaire)
    _need(questionnaire["is_demo"] is False, "questionnaire_is_demo")
    public = zones["zone_1_subject_public"]
    packet_path = build / "packets" / f"{topic_id}_full.json"
    packet = validate_packet(_parse_json(packet_path.read_bytes()), allow_demo=False)
    interventions = {}
    for arm in ARMS:
        interventions[arm] = {}
        for round_no in INTERVENTION_ROUNDS:
            item = public["intervention_packets"][arm][str(round_no)]
            interventions[arm][round_no] = {
                "visible_text": item["visible_text"], "char_count": item["char_count"],
                "content_origin": item["content_origin"],
                "visible_text_sha256": _sha_text(item["visible_text"]),
                "live_run_id": (item.get("live_provenance") or {}).get("run_id")}
    _need(all(interventions["I4"][r]["content_origin"] == "live_model_organized_output"
              for r in INTERVENTION_ROUNDS), "i4_not_live_step39_output")
    block_seed = spec["block_seed"]
    population = scheduling.make_population(8, block_seed)
    index = {agent["agent_id"]: n for n, agent in enumerate(population)}
    stances = {agent["agent_id"]: agent["initial_stance"] for agent in population}
    first, second = spec["subjects"]
    _need(stances[first] == -stances[second], "subject_stance_unexpected")
    if topic_id == "TOPIC_01":
        _need(spec["subjects"] == SUBJECTS and stances["S01"] == 1, "topic01_subjects_changed")
    schedules = {}
    for subject in spec["subjects"]:
        n = index[subject]
        exposure = scheduling.schedule_cards(public["exposure_cards"], stances[subject], ENVIRONMENT,
                                             EXPOSURE_ROUNDS, (block_seed + n) % 6, max_repeats=2)
        retention = scheduling.schedule_cards(public["retention_cards"], stances[subject], ENVIRONMENT,
                                              RETENTION_ROUNDS, (block_seed + n + 3) % 6, max_repeats=2)
        schedules[subject] = {
            "exposure": [[scheduling.public_card(card) for card in batch] for batch in exposure],
            "retention": [[scheduling.public_card(card) for card in batch] for batch in retention]}
    collisions = [row for row in public.get("dose_arm_collisions", []) if row["arm"] in ARMS]
    return {"build": build, "questionnaire": questionnaire, "gold": gold, "packet": packet,
            "packet_sha256": canonical_hash(packet), "interventions": interventions,
            "stances": stances, "schedules": schedules, "collisions": collisions,
            "axis": packet["stance_axis"], "topic": packet["topic"], "spec": spec}


def _persona(materials, subject):
    axis = materials["axis"]
    side = axis["positive_label"] if materials["stances"][subject] > 0 else axis["negative_label"]
    return (f"你是一名普通的社交平台用户，正在参与关于“{materials['topic']}”的公共讨论。在“{axis['name']}”"
            f"这个问题上，你原本更倾向于“{side}”这一侧。请保持普通人的口吻，只依据你实际读到的内容思考，"
            "不编造没读过的材料，也不要假装自己是专家。")


def _single_line(text):
    """测量上下文契约禁止历史中的控制字符：换行确定性渲染为“ ｜ ”，不改其余字符。"""
    return re.sub(r"\s*\n+\s*", " ｜ ", text)


def _cards_message(round_label, cards):
    lines = [f"{round_label}（共{len(cards)}条公开内容）："]
    lines += [f"[{card['card_id']}] {card['text']}" for card in cards]
    return " ｜ ".join(lines)


def _flatten(messages, final_note=None):
    """确定性展开：全部 system 合并；非 system 历史作为带标签转录；末条 user 为当前任务。

    final_note（可选）追加在当前任务之后，用于在长转录后重申输出格式。"""
    systems = [m["content"] for m in messages if m["role"] == "system"]
    others = [m for m in messages if m["role"] != "system"]
    _need(others and others[-1]["role"] == "user", "flatten_requires_final_user")
    parts = []
    for message in others[:-1]:
        tag = "【你读到的内容】" if message["role"] == "user" else "【你自己发表过的话】"
        parts.append(tag + "\n" + message["content"])
    body = ("以下是你此前的经历记录（按时间顺序）：\n\n" + "\n\n".join(parts) + "\n\n") if parts else ""
    return [{"role": "system", "content": "\n\n".join(systems)},
            {"role": "user", "content": body + "【当前任务】\n" + others[-1]["content"]
             + ("\n\n【输出格式提醒】\n" + final_note if final_note else "")}]


# ------------------------------------------------------------------ 状态
def _world(world_id, agents, timepoint, round_no, logical_seq, histories, exposure, speeches, persona):
    return {"world_id": world_id, "agents": [{"agent_id": a} for a in agents],
            "timepoint": timepoint, "round": round_no, "logical_seq": logical_seq,
            "is_demo": False, "messages": deepcopy(histories), "exposure_history": deepcopy(exposure),
            "routine_speech_history": deepcopy(speeches), "system_prompt": persona,
            "measurement_writes_to_memory": False}


def _state_core(state):
    """父子分叉一致性只比较可变核心，排除世界标识。"""
    return canonical_hash({k: v for k, v in state.items() if k not in ("world_id",)})


# ------------------------------------------------------------------ 校验器
def _feedback(code, details):
    """把被拒原因翻译成可执行指令（只给事实与边界，不替模型改写文本）。"""
    instruction = ("上一次输出未通过本地严格校验，请仅修正下列问题后重新完整输出；"
                   "不要附加解释，不要改变已正确的部分。")
    details = details or {}
    if code == "live_micro_pilot:text_length_out_of_range" and {"length", "min", "max"} <= set(details):
        length, low, high = details["length"], details["min"], details["max"]
        target = (low + high) // 2
        if length > high:
            instruction = (f"上一次整段共{length}字，超过上限{high}字{length - high}字。请重写为约{target}字"
                           f"（必须在{low}到{high}字之间，宁短勿长），删去重复与次要的限定语，保持原有观点、"
                           f"依据与疑问不变，仍为一段连续中文纯文本、不出现英文字母。")
        else:
            instruction = (f"上一次整段共{length}字，低于下限{low}字。请扩写为约{target}字"
                           f"（必须在{low}到{high}字之间），补充你的依据或拿不准之处，仍为一段连续中文纯文本。")
    elif code == "live_micro_pilot:latin_letters_forbidden":
        letters = "、".join(details.get("letters") or [])
        instruction = (f"上一次输出含英文字母（{letters}）。请把这些字母改成中文表述（例如“p值”改为“概率”），"
                       f"全文不得出现任何英文字母，其余内容与篇幅尽量保持不变。")
    elif details.get("json_parse_failed"):
        instruction = ("上一次输出不是JSON，而是一段普通文字（像发帖或复述经历）。本次是填写问卷："
                       "只输出一个以“{”开头、以“}”结尾的JSON对象，字段为answers、evidence_notes、"
                       "opposing_view，answers覆盖全部题目；不要输出任何JSON之外的文字。")
    elif details.get("quotes_not_exact_substring"):
        quotes = "；".join("“" + q + "”" for q in details["quotes_not_exact_substring"])
        instruction = ("上一次以下 user_quote 不是用户发言原文的逐字连续片段：" + quotes + "。"
                       "user_quote 必须从 user_view 原文中原样复制一段连续文字：不得删字、改字、合并句子，"
                       "也不得在截断处自行补“。”等标点；最稳妥的做法是复制原文中以原有标点结尾的完整一句。"
                       "其余内容与结构保持不变，只输出一个 JSON 对象。")
    elif details.get("non_json_or_extra_text"):
        instruction = ("上一次输出不是单个严格 JSON 对象（前后混入了契约复述或其他文字）。"
                       "本次只输出一个以“{”开头、以“}”结尾的 JSON 对象，不要复述契约、不要任何说明文字。")
    elif details.get("evidence_based_items_missing_quote_in_sections"):
        instruction = ("上一次有 evidence_based 项缺少 user_quote。每个 evidence_based 项都必须带 user_quote，"
                       "且须从 user_view 原文逐字复制一段连续文字；其余内容保持不变，只输出一个 JSON 对象。")
    elif code == "live_micro_pilot:text_contains_newline":
        instruction = "上一次输出含换行。请合并为一段不换行的连续中文纯文本，其余内容不变。"
    return {"rejected_error_code": code, "diagnostics": details, "instruction": instruction}


def _measure_validator(materials, visible, timepoint):
    q, gold = materials["questionnaire"], materials["gold"]
    expected = {i["item_id"] for name in ("anchor_items", "transfer_items") for i in q[name]}

    def check(raw):
        diag = {}
        stripped = raw.strip()
        if stripped.startswith("```"):
            diag["code_fence_detected"] = True
        try:
            record = measurement.parse_subject_measurement_output(
                raw, q, visible_card_ids=visible, timepoint=timepoint)
        except ValueError as exc:
            try:
                data = json.loads(raw)
                cites = {c for a in data.get("answers", []) for c in a.get("cited_card_ids", [])}
                unseen = sorted(cites - set(visible))
                if unseen:
                    diag["cited_unseen_card_ids"] = unseen[:10]
                    diag["allowed_card_id_count"] = len(visible)
            except (ValueError, AttributeError, TypeError):
                diag["json_parse_failed"] = True
            return None, str(exc), diag
        answered = {a["item_id"] for a in record["output"]["answers"]}
        if answered != expected:
            return None, "live_micro_pilot:answers_incomplete", {
                "missing_item_ids": sorted(expected - answered)}
        try:
            measurement.build_blind_evaluation_packet(record, gold, blind_sample_id="BLIND_9999")
        except ValueError as exc:
            return None, str(exc), {"field_scope": "evidence_notes/opposing_view 含英文缩写、编号或条件词"}
        return record, None, {}
    return check


def _plain_validator(low, high, extra=None):
    def check(raw):
        if "\n" in raw or "\r" in raw:
            return None, "live_micro_pilot:text_contains_newline", {"length": len(raw)}
        if not low <= len(raw) <= high:
            return None, "live_micro_pilot:text_length_out_of_range", {
                "length": len(raw), "min": low, "max": high}
        try:
            _plain_text(raw, "text", maximum=high, minimum=low)
            measurement._text(raw)
        except ValueError as exc:
            return None, str(exc), {"length": len(raw)}
        if re.search(r"[A-Za-z]", raw):
            return None, "live_micro_pilot:latin_letters_forbidden", {
                "letters": sorted(set(re.findall(r"[A-Za-z]+", raw)))[:8]}
        if extra is not None:
            problem = extra(raw)
            if problem:
                return None, problem[0], problem[1]
        return raw, None, {}
    return check


def _mirror_diagnostics(raw_text, user_view):
    """只诊断、不修复：指出非逐字引句与 JSON 外多余文字，供重试反馈使用。"""
    diag = {}
    try:
        data = json.loads(raw_text)
    except ValueError:
        stripped = raw_text.lstrip()
        diag["non_json_or_extra_text"] = True
        diag["starts_with_brace"] = stripped.startswith("{")
        return diag
    bad, missing = [], []
    for section in (data.get("sections") or []) if isinstance(data, dict) else []:
        for item in (section.get("items") or []) if isinstance(section, dict) else []:
            if not isinstance(item, dict):
                continue
            quote = item.get("user_quote")
            if isinstance(quote, str) and quote and quote not in user_view:
                bad.append(quote[:80])
            if item.get("epistemic_status") == "evidence_based" and not quote:
                missing.append(section.get("name"))
    if bad:
        diag["quotes_not_exact_substring"] = bad[:5]
    if missing:
        diag["evidence_based_items_missing_quote_in_sections"] = missing[:5]
    return diag


def _mirror_validator(packet, user_view):
    def check(raw_response):
        try:
            response = _response_metadata(raw_response, 200000, expected_demo=False)
            parsed = parse_stage_output(response["raw_text"], "mirror", packet, user_view=user_view)
        except ValueError as exc:
            raw_text = raw_response.get("raw_text", "") if isinstance(raw_response, dict) else ""
            return None, f"contracts:{getattr(exc, 'code', '') or exc}", _mirror_diagnostics(raw_text, user_view)
        return parsed, None, {}
    return check


def _blind_validator(packet):
    def check(raw):
        try:
            return measurement.parse_blind_evaluation_output(raw, packet), None, {}
        except ValueError as exc:
            return None, str(exc), {}
    return check


# ------------------------------------------------------------------ 调用
class _Runner:
    def __init__(self, store, transport, run_id, settings):
        self.store, self.transport, self.run_id, self.settings = store, transport, run_id, settings
        self.nodes = []

    def call(self, node_id, kind, messages, validator, *, max_tokens, meta, envelope=False):
        """最多 3 次；被拒尝试附带错误码与诊断重发。返回 (value, node)。"""
        node = {"node_id": node_id, "kind": kind, **meta, "attempts": [], "accepted": False,
                "accepted_attempt": None, "accepted_raw_text": None, "accepted_response_sha256": None}
        self.nodes.append(node)
        feedback = None
        settings = dict(self.settings, max_tokens=max_tokens)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wire = deepcopy(messages)
            if feedback is not None:
                wire[-1] = {"role": wire[-1]["role"], "content": wire[-1]["content"]
                            + "\n\n【上一次输出的校验反馈】\n" + json.dumps(feedback, ensure_ascii=False)}
            request = {"request_id": f"{self.run_id}:{node_id}:{attempt}", "stage": kind,
                       "is_demo": False, "model_settings": settings, "messages": wire}
            request_ref = self.store.capture(request)
            self.store.append("micro_request_prepared", task_id=request["request_id"],
                              input_ref=request_ref["ref"], input_sha256=request_ref["sha256"],
                              payload={"node_id": node_id, "attempt": attempt, "feedback": feedback})
            row = {"attempt": attempt, "request_id": request["request_id"],
                   "request_sha256": request_ref["sha256"]}
            node["attempts"].append(row)
            try:
                response = self.transport.complete(request)
            except (MiMoError, BudgetError) as exc:
                row.update(status="transport_error", error=getattr(exc, "code", type(exc).__name__))
                self.store.append("micro_attempt_aborted", task_id=request["request_id"],
                                  payload={"node_id": node_id, "attempt": attempt, "error": row["error"]})
                raise
            response_ref = self.store.capture(response)
            row["response_sha256"] = response_ref["sha256"]
            row["usage"] = response["usage"]
            value, error, diag = validator(response if envelope else response["raw_text"])
            if error is None:
                row.update(status="accepted")
                event = self.store.append("micro_attempt_accepted", task_id=request["request_id"],
                                          input_ref=request_ref["ref"], input_sha256=request_ref["sha256"],
                                          output_ref=response_ref["ref"], output_sha256=response_ref["sha256"],
                                          payload={"node_id": node_id, "attempt": attempt})
                row["event_id"] = event["event_id"]
                node.update(accepted=True, accepted_attempt=attempt,
                            accepted_raw_text=response["raw_text"],
                            accepted_response_sha256=response_ref["sha256"],
                            accepted_event_id=event["event_id"])
                return value, node
            row.update(status="rejected", error=error, diagnostics=diag)
            event = self.store.append("micro_attempt_rejected", task_id=request["request_id"],
                                      input_ref=request_ref["ref"], input_sha256=request_ref["sha256"],
                                      output_ref=response_ref["ref"], output_sha256=response_ref["sha256"],
                                      payload={"node_id": node_id, "attempt": attempt, "error": error,
                                               "diagnostics": diag})
            row["event_id"] = event["event_id"]
            feedback = _feedback(error, diag)
        raise ValueError(f"live_micro_pilot:node_failed:{node_id}")


def _settings(config):
    model = config["models"]["assistant"]
    return {key: model[key] for key in ("provider", "model", "temperature", "max_tokens")}


def run_topic01_live_micro_pilot(evidence_build_dir=None, *, execute=False, config=None, root=None,
                                 runs_root=None, manifest_path=None) -> dict:
    """向后兼容入口：TOPIC_01。"""
    return run_topic_live_micro_pilot("TOPIC_01", evidence_build_dir, execute=execute, config=config,
                                      root=root, runs_root=runs_root, manifest_path=manifest_path)


def run_topic_live_micro_pilot(topic_id, evidence_build_dir=None, *, execute=False, config=None, root=None,
                               runs_root=None, manifest_path=None) -> dict:
    """真实执行指定议题完整微型实证链路；未授权或未就绪时 0 调用返回 blocked。"""
    spec = topic_spec(topic_id)
    repo = Path(root).resolve() if root is not None else ROOT
    if config is None:
        from .configuration import load_config
        config = load_config(repo / "configs/core.json")
    status = mimo_status(config, repo)
    base = {"schema_version": SCHEMA_VERSION, "contract": spec["contract"], "topic_id": topic_id,
            "executed": False, "status": "blocked",
            "blockers": [], "actual_model_calls": 0, "cost_cny": "0.00000000",
            "billing_before": status.get("billing")}
    if status["blockers"]:
        return {**base, "blockers": list(status["blockers"])}
    if execute is not True:
        return {**base, "blockers": ["execute_required"]}

    materials = load_materials(evidence_build_dir, topic_id)
    settings = _settings(config)
    ledger = _ledger(config, repo)
    billing_before = ledger.status()
    parent = Path(runs_root) if runs_root is not None else repo / RUNS_RELATIVE
    parent.mkdir(parents=True, exist_ok=True)
    run_id = (spec["run_prefix"] + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
              + "_" + uuid.uuid4().hex[:8])
    run_dir = parent / run_id
    run_dir.mkdir()
    store = EventStore(run_dir, {"contract": spec["contract"], "model_settings": settings, "topic_id": topic_id,
                                 "block_id": BLOCK_ID, "environment": ENVIRONMENT, "subjects": list(spec["subjects"]),
                                 "arms": list(ARMS), "flatten_rule": FLATTEN_RULE,
                                 "registry_seed": REGISTRY_SEED, "thinking": "disabled"},
                       materials["packet_sha256"], False, run_id)
    transport = MiMoTransport(config["models"]["assistant"], store, ledger)
    runner = _Runner(store, transport, run_id, settings)
    store.append("micro_pilot_started", payload={"billing_before": billing_before,
                 "scope": "live_micro_pilot_n2_x3_not_formal"})
    record = {"run_id": run_id, "run_directory": run_dir.as_posix(), "status": "running", "error": None}
    state = {}
    try:
        state = _execute(materials, runner, store, transport, config, repo, run_id, settings)
        record["status"] = "completed"
    except (MiMoError, BudgetError, ValueError) as exc:
        record.update(status="incomplete", error={"code": getattr(exc, "code", None) or str(exc)[:200]})
        store.append("micro_pilot_failed", payload=record["error"])
    billing_after = ledger.status()
    # charged_cny 只计已结算请求；存在“中断持有”预留时 total_cost_cny 为 null，故不用它。
    cost = "%.8f" % (float(billing_after["charged_cny"]) - float(billing_before["charged_cny"]))
    summary = {"schema_version": SCHEMA_VERSION, "contract": spec["contract"], "topic_id": topic_id,
               "subjects": list(spec["subjects"]), "subject_rule": SUBJECT_RULE,
               "prompt_revision": PROMPT_REVISION, "feedback_revision": FEEDBACK_REVISION, **record,
               "actual_model_calls": transport.actual_model_calls, "nodes": runner.nodes,
               "billing_before": billing_before, "billing_after": billing_after, "cost_cny": cost,
               "model_settings": settings, "flatten_rule": FLATTEN_RULE, "completed_at_utc": utc_now(),
               **state}
    store.append("micro_pilot_completed" if record["status"] == "completed" else "micro_pilot_stopped",
                 payload={"status": record["status"], "actual_model_calls": transport.actual_model_calls,
                          "cost_cny": cost})
    summary["verification"] = verify_events(run_dir)
    write_json_new(run_dir / "summary.json", summary)
    result = {**base, "executed": True, "status": record["status"], "run_id": run_id,
              "run_directory": run_dir.as_posix(), "actual_model_calls": transport.actual_model_calls,
              "cost_cny": cost, "billing_after": billing_after, "error": record["error"]}
    if record["status"] == "completed":
        result["manifest"] = build_topic_live_micro_pilot_manifest(
            run_dir, repo_root=repo, manifest_path=manifest_path)
    return result


def _execute(materials, runner, store, transport, config, repo, run_id, settings):
    q, gold, packet = materials["questionnaire"], materials["gold"], materials["packet"]
    spec = materials["spec"]
    SUBJECTS, PARENT_WORLD, TOPIC_ID = spec["subjects"], spec["parent_world"], spec["topic_id"]
    personas = {s: _persona(materials, s) for s in SUBJECTS}
    histories = {s: [] for s in SUBJECTS}
    exposure_events, speeches = [], []
    measurements = {}   # (subject, arm|None, timepoint) -> record

    def measure(world_state, subject, arm, timepoint, world_id):
        ctx = measurement.prepare_subject_measurement_context(world_state, subject, q)
        _need(ctx["timepoint"] == timepoint, "context_timepoint_mismatch")
        messages = ctx["messages"][:-1] + [{"role": "system", "content": MEASURE_GUIDE}, ctx["messages"][-1]]
        node_id = f"measure_{subject}_{arm or 'P'}_{timepoint}"
        record, node = runner.call(node_id, "subject_measurement", _flatten(messages, MEASURE_FORMAT_NOTE),
                                   _measure_validator(materials, ctx["visible_card_ids"], timepoint),
                                   max_tokens=3072, meta={"subject": subject, "arm": arm,
                                   "timepoint": timepoint, "world_id": world_id,
                                   "visible_card_ids": ctx["visible_card_ids"],
                                   "context_sha256": canonical_hash(ctx["messages"])})
        measurements[(subject, arm, timepoint)] = {"record": record, "world_id": world_id,
                                                   "node_id": node_id}
        return record

    # 阶段1：T0（零曝光）
    for subject in SUBJECTS:
        world = _world(PARENT_WORLD, SUBJECTS, "T0", 0, 0, histories, exposure_events, speeches,
                       personas[subject])
        measure(world, subject, None, "T0", PARENT_WORLD)

    # 阶段2：8 轮环境 D 曝光（无模型调用），T1 常规发言
    seq = 0
    for round_no in range(1, EXPOSURE_ROUNDS + 1):
        for subject in SUBJECTS:
            cards = materials["schedules"][subject]["exposure"][round_no - 1]
            seq += 1
            exposure_events.append({"event_id": f"EXP_{subject}_R{round_no:02d}", "event_type": "exposure_delivered",
                                    "agent_id": subject, "status": "delivered", "sent_to_model": True,
                                    "cards": deepcopy(cards)})
            histories[subject].append({"role": "user",
                                       "content": _cards_message(f"第{round_no}轮信息流", cards)})
    store.append("micro_exposure_delivered", payload={
        "rounds": EXPOSURE_ROUNDS, "deliveries": {s: sum(len(b) for b in materials["schedules"][s]["exposure"])
                                                  for s in SUBJECTS},
        "card_ids": {s: [[c["card_id"] for c in b] for b in materials["schedules"][s]["exposure"]]
                     for s in SUBJECTS}})
    task = SPEECH_TASK.format(low=SPEECH_RANGE[0], high=SPEECH_RANGE[1], target=SPEECH_TARGET)
    for n, subject in enumerate(SUBJECTS, 1):
        messages = [{"role": "system", "content": personas[subject]}] + histories[subject] + [
            {"role": "user", "content": task}]

        def routine_check(text, subject=subject, n=n):
            row = {"speech_id": f"SP_LIVE_{subject}", "event_id": f"RS_LIVE_{subject}", "world_id": PARENT_WORLD,
                   "agent_id": subject, "topic_id": TOPIC_ID, "phase": "exposure", "round": 8,
                   "logical_seq": 800 + n, "event_type": "routine_speech_generated", "role": "assistant",
                   "status": "completed", "generated": True, "text": text, "is_demo": False}
            probe = _world(PARENT_WORLD, SUBJECTS, "T1", 8, 900, histories, exposure_events, [row], personas[subject])
            try:
                measurement.extract_t1_routine_speech_view(probe, subject, topic_id=TOPIC_ID)
            except ValueError as exc:
                return str(exc), {"scope": "常规发言含编号、英文或测量措辞"}
            return None

        speech, node = runner.call(f"speech_{subject}_T1", "routine_speech", _flatten(messages),
                                   _plain_validator(*SPEECH_RANGE, extra=routine_check), max_tokens=1024,
                                   meta={"subject": subject, "arm": None, "timepoint": "T1",
                                         "world_id": PARENT_WORLD})
        speeches.append({"speech_id": f"SP_LIVE_{subject}", "event_id": f"RS_LIVE_{subject}",
                         "world_id": PARENT_WORLD, "agent_id": subject, "topic_id": TOPIC_ID,
                         "phase": "exposure", "round": 8, "logical_seq": 800 + n,
                         "event_type": "routine_speech_generated", "role": "assistant", "status": "completed",
                         "generated": True, "text": speech, "is_demo": False})
        histories[subject].append({"role": "user", "content": task})
        histories[subject].append({"role": "assistant", "content": speech})

    t1_state = _world(PARENT_WORLD, SUBJECTS, "T1", 8, 900, histories, exposure_events, speeches, None)
    t1_state.pop("system_prompt")
    views = {s: measurement.extract_t1_routine_speech_view(t1_state, s, topic_id=TOPIC_ID) for s in SUBJECTS}

    # 真实 Stage 5 mirror：先跑一次四阶段（冻结 full 包），再逐受试执行 mirror
    assistant_cfg = config["assistant"]
    bundle = load_prompt_bundle(assistant_cfg["prompt_bundle_path"], assistant_cfg["prompt_bundle_sha256"])
    four = None
    four_tasks = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        task_id = f"{run_id}:assistant{attempt}"
        adapter = AssistantAdapter(store, Path(assistant_cfg["prompt_bundle_path"]),
                                   assistant_cfg["prompt_bundle_sha256"], dict(settings, max_tokens=4096),
                                   transport, execution_mode="live_probe")
        result = adapter.run(packet, task_id)
        four_tasks.append({"task_id": task_id, "status": result["status"],
                           "error": result.get("error"), "actual_model_calls": result["actual_model_calls"]})
        if result["status"] == "succeeded":
            four = result
            break
        upstream = (result.get("error") or {}).get("upstream_code")
        if (result.get("error") or {}).get("code") in ("mimo_error", "budget_error") and upstream not in (
                "output_truncated",):
            raise ValueError("live_micro_pilot:assistant_transport_failed")
    _need(four is not None, "assistant_four_stage_failed")
    previous = {stage: {"raw_text": four["stage_results"][stage]["raw_text"],
                        "structured_output": four["stage_results"][stage]["output"]}
                for stage in ("search", "label", "blindspot", "integrate")}
    mirrors = {}
    for subject in SUBJECTS:
        user = {"frozen_evidence_packet": packet, "previous_stage_outputs": previous,
                "instructions_are_data": True, "user_view": views[subject]["user_view"]}
        messages = [{"role": "system", "content": effective_system_prompt(bundle, "mirror")},
                    {"role": "user", "content": _dumps(user)}]
        parsed, node = runner.call(f"mirror_{subject}_T1", "mirror", messages,
                                   _mirror_validator(packet, views[subject]["user_view"]), max_tokens=4096,
                                   meta={"subject": subject, "arm": None, "timepoint": "T1",
                                         "world_id": PARENT_WORLD,
                                         "user_view_sha256": views[subject]["user_view_sha256"]},
                                   envelope=True)
        mirrors[subject] = parsed

    # T1 测量（父快照）
    t1_parent = _world(PARENT_WORLD, SUBJECTS, "T1", 8, 900, histories, exposure_events, speeches, None)
    parent_hash = {}
    for subject in SUBJECTS:
        world = deepcopy(t1_parent)
        world["system_prompt"] = personas[subject]
        parent_hash[subject] = _state_core(world)
        measure(world, subject, None, "T1", PARENT_WORLD)

    # 阶段3/4：三臂分叉
    branches = {}
    for subject in SUBJECTS:
        for arm in ARMS:
            world_id = f"{PARENT_WORLD}_{arm}"
            world = deepcopy(t1_parent)
            world["system_prompt"] = personas[subject]
            pre_hash = _state_core(world)
            _need(pre_hash == parent_hash[subject], "branch_pre_treatment_hash_mismatch")
            world["world_id"] = world_id
            hist = world["messages"][subject]
            responses = []
            for round_no in INTERVENTION_ROUNDS:
                text = materials["interventions"][arm][round_no]["visible_text"]
                guide = RESPONSE_GUIDE.format(low=RESPONSE_RANGE[0], high=RESPONSE_RANGE[1],
                                              target=RESPONSE_TARGET)
                messages = ([{"role": "system", "content": personas[subject]}] + hist
                            + [{"role": "user", "content": text + "\n\n" + guide}])
                reply, node = runner.call(f"respond_{subject}_{arm}_R{round_no}", "intervention_response",
                                          _flatten(messages), _plain_validator(*RESPONSE_RANGE), max_tokens=1024,
                                          meta={"subject": subject, "arm": arm, "timepoint": "T2",
                                                "world_id": world_id, "intervention_round": round_no,
                                                "intervention_sha256":
                                                    materials["interventions"][arm][round_no]["visible_text_sha256"]})
                hist.append({"role": "user", "content": _single_line(text + "\n\n" + guide)})
                hist.append({"role": "assistant", "content": reply})
                responses.append(reply)
            world["timepoint"] = "T2"
            measure(world, subject, arm, "T2", world_id)
            for round_no in range(1, RETENTION_ROUNDS + 1):
                cards = materials["schedules"][subject]["retention"][round_no - 1]
                world["exposure_history"].append({
                    "event_id": f"RET_{subject}_{arm}_R{round_no:02d}", "event_type": "exposure_delivered",
                    "agent_id": subject, "status": "delivered", "sent_to_model": True, "cards": deepcopy(cards)})
                hist.append({"role": "user", "content": _cards_message(f"保持期第{round_no}轮信息流", cards)})
            world["timepoint"] = "T3"
            measure(world, subject, arm, "T3", world_id)
            branches[(subject, arm)] = {"world_id": world_id, "pre_treatment_core_sha256": pre_hash,
                                        "responses": responses, "final_state_sha256": canonical_hash(world)}

    # 匿名注册表与独立盲评
    entries = []
    for (subject, arm, timepoint), item in measurements.items():
        entries.append({"parsed_measurement": item["record"], "gold_bundle": gold,
                        "world_id": item["world_id"], "agent_id": subject, "environment": ENVIRONMENT,
                        "arm": arm, "timepoint": timepoint, "block_id": BLOCK_ID})
    registry = measurement.create_blind_sample_registry(entries, registry_seed=REGISTRY_SEED)
    evaluations = []
    for blind_packet in registry["public_blind_packets"]:
        sid = blind_packet["blind_sample_id"]
        messages = [{"role": "system", "content": BLIND_SYSTEM},
                    {"role": "user", "content": json.dumps(blind_packet, ensure_ascii=False)}]
        evaluation, node = runner.call(f"blind_{sid}", "blind_evaluation", messages,
                                       _blind_validator(blind_packet), max_tokens=2048,
                                       meta={"blind_sample_id": sid, "subject": None, "arm": None,
                                             "timepoint": None, "world_id": None})
        evaluations.append(evaluation)
    by_id = {sid: registry["source_entries"][bound["source_entry_index"]]["parsed_measurement"]
             for sid, bound in registry["private_mapping"].items()}
    panel = measurement.reconcile_blind_evaluations(registry, evaluations, by_id, gold,
                                                    expected_registry_sha256=registry["registry_sha256"])
    write_json_new(Path(store.root) / "controller_private_registry.json", registry)
    return {
        "four_stage_tasks": four_tasks, "four_stage_task_id": four["task_id"],
        "user_views": {s: {k: views[s][k] for k in ("user_view", "user_view_sha256", "speech_ids")}
                       for s in SUBJECTS},
        "mirrors": {s: mirrors[s]["output"] for s in SUBJECTS},
        "mirror_citation_audit": {s: mirrors[s]["citation_audit"] for s in SUBJECTS},
        "parent_t1_core_sha256": parent_hash,
        "branches": [{"subject": s, "arm": a, **v} for (s, a), v in branches.items()],
        "registry_sha256": registry["registry_sha256"], "registry_seed": REGISTRY_SEED,
        "panel": panel, "panel_sha256": panel["panel_sha256"],
        "collisions": materials["collisions"],
    }


# ------------------------------------------------------------------ 清单
def _ledger_rows(repo):
    path = Path(repo) / LEDGER_RELATIVE
    return [_parse_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ledger_totals(rows, run_id):
    settles = [r for r in rows if r.get("type") == "settle" and str(r.get("request_id", "")).startswith(run_id + ":")]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for row in settles:
        for key in usage:
            usage[key] += int((row.get("usage") or {}).get(key) or 0)
    return {"settle_count": len(settles), "charged_cny": "%.8f" % sum(float(r["charged_cny"]) for r in settles),
            "usage": usage}


def _superseded_runs(repo, rows, current_run_id, topic_id=TOPIC_ID):
    """同议题内被取代的真实运行（失败/中断）：只读其 aborted.json 与账本，费用如实并入。

    只收本议题前缀的运行，保证新增议题的失败运行不会改变 TOPIC_01 冻结清单的核验结果。"""
    out = []
    root = Path(repo) / RUNS_RELATIVE
    for path in sorted(root.glob(f"live_micro_pilot_{topic_id}_*/aborted.json")):
        info = _parse_json(path.read_bytes())
        run_id = info["run_id"]
        if run_id == current_run_id:
            continue
        totals = _ledger_totals(rows, run_id)
        uncertain = [r for r in rows if r.get("type") == "uncertain"
                     and str(r.get("request_id", "")).startswith(run_id + ":")]
        out.append({"run_id": run_id, "status": info["status"], "reason": info["reason"],
                    "settle_count": totals["settle_count"], "charged_cny": totals["charged_cny"],
                    "usage": totals["usage"],
                    "uncertain_requests": [{"request_id": r["request_id"], "reason": r["reason"]}
                                           for r in uncertain],
                    "aborted_record_sha256": canonical_hash(info)})
    return out


def _table(panel_rows, subjects=SUBJECTS):
    """按受试×臂×时点组织指标；T0/T1 为三臂共享父级测量。"""
    index = {}
    for row in panel_rows:
        index[(row["agent_id"], row["arm"], row["timepoint"])] = row
    out = []
    for subject in subjects:
        for arm in ARMS:
            for timepoint in ("T0", "T1", "T2", "T3"):
                row = index.get((subject, None if timepoint in ("T0", "T1") else arm, timepoint))
                out.append({"subject": subject, "arm": arm, "timepoint": timepoint,
                            "shared_parent": timepoint in ("T0", "T1"),
                            "blind_sample_id": row["blind_sample_id"],
                            **{k: row[k] for k in ("BS", "Acc", "S", "O", "U")},
                            "transfer_BS": (row["transfer"] or {}).get("BS")})
    return out


def build_topic01_live_micro_pilot_manifest(run_directory, *, repo_root=None, manifest_path=None) -> dict:
    """向后兼容入口。"""
    return build_topic_live_micro_pilot_manifest(run_directory, repo_root=repo_root, manifest_path=manifest_path)


def build_topic_live_micro_pilot_manifest(run_directory, *, repo_root=None, manifest_path=None) -> dict:
    """从运行目录 summary 汇总可核验清单（不含私有金标准，只含哈希与公开文本）。"""
    repo = Path(repo_root).resolve() if repo_root is not None else ROOT
    run_dir = Path(run_directory).resolve(strict=True)
    summary = _parse_json((run_dir / "summary.json").read_bytes())
    _need(summary["status"] == "completed", "run_not_completed")
    topic_id = summary.get("topic_id", TOPIC_ID)
    spec = topic_spec(topic_id)
    _need(summary["run_id"].startswith(spec["run_prefix"]), "run_topic_mismatch")
    ledger_rows = _ledger_rows(repo)
    ledger = _ledger_totals(ledger_rows, summary["run_id"])
    superseded = _superseded_runs(repo, ledger_rows, summary["run_id"], topic_id)
    panel = summary["panel"]
    nodes = []
    for node in summary["nodes"]:
        nodes.append({k: node.get(k) for k in (
            "node_id", "kind", "subject", "arm", "timepoint", "world_id", "blind_sample_id",
            "accepted", "accepted_attempt", "accepted_response_sha256", "accepted_event_id",
            "visible_card_ids", "intervention_round", "intervention_sha256")}
            | {"attempts": [{k: a.get(k) for k in ("attempt", "request_id", "status", "error",
                                                    "response_sha256", "usage")} for a in node["attempts"]],
               "accepted_text": node["accepted_raw_text"] if node["kind"] in (
                   "routine_speech", "intervention_response") else None})
    measured = {}
    for node in summary["nodes"]:
        if node["kind"] == "subject_measurement" and node["accepted"]:
            output = json.loads(node["accepted_raw_text"])
            measured[node["node_id"]] = {"opposing_view": output["opposing_view"],
                                         "evidence_notes": output["evidence_notes"]}
    rejected = sum(1 for n in summary["nodes"] for a in n["attempts"] if a["status"] == "rejected")
    manifest = {
        "schema_version": SCHEMA_VERSION, "contract": spec["contract"], "created_at_utc": utc_now(),
        "run_id": summary["run_id"], "run_directory": run_dir.as_posix(),
        "design": {"topic_id": topic_id, "block_id": BLOCK_ID, "block_seed": spec["block_seed"],
                   "environment": ENVIRONMENT, "subjects": list(spec["subjects"]), "arms": list(ARMS),
                   "exposure_rounds": EXPOSURE_ROUNDS, "cards_per_round": 6,
                   "exposure_card_deliveries_per_subject": EXPOSURE_ROUNDS * 6,
                   "retention_rounds": RETENTION_ROUNDS, "intervention_rounds": list(INTERVENTION_ROUNDS),
                   "measurements": 16, "blind_evaluations": 16, "registry_seed": REGISTRY_SEED,
                   "flatten_rule": FLATTEN_RULE, "prompt_revision": PROMPT_REVISION,
                   "mirror_delivered_to_branches": False,
                   "condition_labels": CONDITION_LABELS,
                   **({} if topic_id == TOPIC_ID else {
                       "topic_label": spec["label"], "subject_rule": SUBJECT_RULE,
                       "subject_stances": spec["stances"],
                       "feedback_revision": summary.get("feedback_revision")})},
        "model_settings": summary["model_settings"],
        "four_stage": {"task_id": summary["four_stage_task_id"], "attempts": summary["four_stage_tasks"]},
        "nodes": nodes,
        "user_views": summary["user_views"], "mirrors": summary["mirrors"],
        "mirror_citation_audit": summary["mirror_citation_audit"],
        "measurement_texts": measured,
        "parent_t1_core_sha256": summary["parent_t1_core_sha256"], "branches": summary["branches"],
        "registry_sha256": summary["registry_sha256"], "panel_sha256": summary["panel_sha256"],
        "panel_records": [{k: r[k] for k in ("blind_sample_id", "agent_id", "arm", "timepoint", "world_id",
                                              "BS", "Acc", "S", "O", "U", "objective_complete", "row_complete",
                                              "measurement_output_sha256", "blind_evaluation_sha256")}
                          for r in panel["panel_records"]],
        "comparison": _table(panel["panel_records"], spec["subjects"]),
        "collisions": summary["collisions"],
        "totals": {"actual_model_calls": summary["actual_model_calls"], "rejected_attempts": rejected,
                   "ledger_settles": ledger["settle_count"], "charged_cny": ledger["charged_cny"],
                   "usage": ledger["usage"], "billing_after_total_cny": summary["billing_after"]["charged_cny"],
                   "billing_after_reserved_cny": summary["billing_after"]["reserved_cny"],
                   "billing_after_remaining_cny": summary["billing_after"]["available_cny"]},
        "superseded_runs": superseded,
        "step_spend": {"final_run_charged_cny": ledger["charged_cny"],
                       "superseded_charged_cny": "%.8f" % sum(float(r["charged_cny"]) for r in superseded),
                       "step_charged_cny": "%.8f" % (float(ledger["charged_cny"])
                                                     + sum(float(r["charged_cny"]) for r in superseded)),
                       "held_interrupted_requests": [u for r in superseded for u in r["uncertain_requests"]],
                       "note": "被取代运行的全部已结算费用如实并入；中断在途请求用量未知，"
                               "按最坏情况预留持有，未伪造结算。"},
        "honest_gates": {"is_demo": False, "sample_size_subjects": 2, "arms": 3,
                         "statistical_inference": "none_mechanism_demo_only", "human_reviews_completed": 0,
                         "full_I4_eligible": False, "approved_for_experiment": False, "formal_eligible": False,
                         "subject_is_language_model": True, "real_human_subjects": 0},
        "event_chain": summary["verification"],
    }
    manifest["manifest_sha256"] = canonical_hash(manifest)
    target = Path(manifest_path) if manifest_path is not None else repo / spec["manifest_relative"]
    target.parent.mkdir(parents=True, exist_ok=True)
    _need(not target.exists(), "manifest_exists")
    write_json_new(target, manifest)
    return {"status": "written", "path": target.as_posix(), "manifest_sha256": manifest["manifest_sha256"]}


# ------------------------------------------------------------------ 离线核验
def verify_topic01_live_micro_pilot(manifest_path=None, *, repo_root=None, materials=None) -> dict:
    """向后兼容入口：TOPIC_01。"""
    return verify_topic_live_micro_pilot("TOPIC_01", manifest_path, repo_root=repo_root, materials=materials)


def verify_topic_live_micro_pilot(topic_id, manifest_path=None, *, repo_root=None, materials=None) -> dict:
    """离线重放：事件链、响应 blob、全部校验器、注册表与面板重建、账本对账、诚实门禁。"""
    repo = Path(repo_root).resolve() if repo_root is not None else ROOT
    spec = topic_spec(topic_id)
    path = Path(manifest_path) if manifest_path is not None else repo / spec["manifest_relative"]
    _need(path.is_file(), "manifest_missing")
    manifest = _parse_json(path.read_bytes())
    _need(canonical_hash({k: v for k, v in manifest.items() if k != "manifest_sha256"})
          == manifest["manifest_sha256"], "manifest_hash_mismatch")
    _need(manifest["design"]["topic_id"] == topic_id, "manifest_topic_mismatch")
    _need(manifest["contract"] == spec["contract"], "manifest_contract_invalid")
    _need(tuple(manifest["design"]["subjects"]) == spec["subjects"], "manifest_subjects_mismatch")
    SUBJECTS = spec["subjects"]
    run_dir = Path(manifest["run_directory"])
    summary = _parse_json((run_dir / "summary.json").read_bytes())
    problems = []
    chain = verify_events(run_dir)
    if chain.get("chain_valid") is not True:
        problems.append("event_chain_invalid")
    materials = materials or load_materials(topic_id=topic_id)
    _need(materials["spec"]["topic_id"] == topic_id, "materials_topic_mismatch")
    q, gold, packet = materials["questionnaire"], materials["gold"], materials["packet"]
    summary_nodes = {n["node_id"]: n for n in summary["nodes"]}
    records = {}
    for node in manifest["nodes"]:
        source = summary_nodes.get(node["node_id"])
        if source is None or not node["accepted"]:
            problems.append("node_missing_or_rejected:" + node["node_id"])
            continue
        blob = _verify_blob(run_dir, {"ref": f"raw/blobs/{node['accepted_response_sha256']}.json",
                                      "sha256": node["accepted_response_sha256"]})
        if blob["raw_text"] != source["accepted_raw_text"]:
            problems.append("raw_text_blob_mismatch:" + node["node_id"])
            continue
        kind, raw = node["kind"], blob["raw_text"]
        if kind == "subject_measurement":
            value, error, _ = _measure_validator(materials, node["visible_card_ids"], node["timepoint"])(raw)
            if error:
                problems.append("measurement_revalidation_failed:" + node["node_id"])
            else:
                records[node["node_id"]] = value
        elif kind in ("routine_speech", "intervention_response"):
            low, high = SPEECH_RANGE if kind == "routine_speech" else RESPONSE_RANGE
            if _plain_validator(low, high)(raw)[1] is not None or raw != node["accepted_text"]:
                problems.append("plain_text_revalidation_failed:" + node["node_id"])
        elif kind == "mirror":
            view = manifest["user_views"][node["subject"]]["user_view"]
            parsed, error, _ = _mirror_validator(packet, view)(blob)
            if error or parsed["output"] != manifest["mirrors"][node["subject"]]:
                problems.append("mirror_revalidation_failed:" + node["node_id"])
    for subject in SUBJECTS:
        view = manifest["user_views"][subject]
        if _sha_text(view["user_view"]) != view["user_view_sha256"]:
            problems.append("user_view_hash_mismatch:" + subject)
        speech = next(n for n in manifest["nodes"] if n["node_id"] == f"speech_{subject}_T1")
        if speech["accepted_text"] not in view["user_view"]:
            problems.append("user_view_not_from_speech:" + subject)
    # 注册表与面板重建
    try:
        entries = []
        for node in manifest["nodes"]:
            if node["kind"] != "subject_measurement" or node["node_id"] not in records:
                continue
            entries.append({"parsed_measurement": records[node["node_id"]], "gold_bundle": gold,
                            "world_id": node["world_id"], "agent_id": node["subject"],
                            "environment": ENVIRONMENT, "arm": node["arm"], "timepoint": node["timepoint"],
                            "block_id": BLOCK_ID})
        registry = measurement.create_blind_sample_registry(entries, registry_seed=manifest["design"]["registry_seed"])
        if registry["registry_sha256"] != manifest["registry_sha256"]:
            problems.append("registry_rebuild_mismatch")
        evaluations = []
        for blind_packet in registry["public_blind_packets"]:
            node = next(n for n in manifest["nodes"] if n.get("blind_sample_id") == blind_packet["blind_sample_id"])
            blob = _verify_blob(run_dir, {"ref": f"raw/blobs/{node['accepted_response_sha256']}.json",
                                          "sha256": node["accepted_response_sha256"]})
            evaluations.append(measurement.parse_blind_evaluation_output(blob["raw_text"], blind_packet))
        by_id = {sid: registry["source_entries"][b["source_entry_index"]]["parsed_measurement"]
                 for sid, b in registry["private_mapping"].items()}
        panel = measurement.reconcile_blind_evaluations(registry, evaluations, by_id, gold,
                                                        expected_registry_sha256=registry["registry_sha256"])
        if panel["panel_sha256"] != manifest["panel_sha256"]:
            problems.append("panel_rebuild_mismatch")
        if _table(panel["panel_records"], SUBJECTS) != manifest["comparison"]:
            problems.append("comparison_table_mismatch")
        if panel["complete_panel_records"] != 16:
            problems.append("panel_incomplete")
    except (ValueError, KeyError, StopIteration) as exc:
        problems.append("registry_or_panel_error:" + str(exc)[:120])
    # 分叉一致性
    for branch in manifest["branches"]:
        if branch["pre_treatment_core_sha256"] != manifest["parent_t1_core_sha256"][branch["subject"]]:
            problems.append("branch_parent_hash_mismatch")
    # 账本
    ledger = _ledger_totals(_ledger_rows(repo), manifest["run_id"])
    if ledger["charged_cny"] != manifest["totals"]["charged_cny"]:
        problems.append("ledger_charge_mismatch")
    if ledger["settle_count"] != manifest["totals"]["ledger_settles"]:
        problems.append("ledger_settle_count_mismatch")
    if manifest["totals"]["actual_model_calls"] != summary["actual_model_calls"]:
        problems.append("call_count_mismatch")
    rows = _ledger_rows(repo)
    if not manifest["run_id"].startswith(spec["run_prefix"]):
        problems.append("run_topic_mismatch")
    if _superseded_runs(repo, rows, manifest["run_id"], topic_id) != manifest.get("superseded_runs"):
        problems.append("superseded_runs_mismatch")
    gates = manifest["honest_gates"]
    for key, expected in (("formal_eligible", False), ("approved_for_experiment", False),
                          ("full_I4_eligible", False), ("human_reviews_completed", 0), ("is_demo", False)):
        if gates.get(key) is not expected:
            problems.append("honest_gate_" + key)
    return {"contract": spec["contract"], "topic_id": topic_id, "passed": not problems, "problems": problems,
            "manifest_sha256": manifest["manifest_sha256"], "run_id": manifest["run_id"],
            "measurements_revalidated": len(records), "panel_records": len(manifest["panel_records"]),
            "charged_cny": ledger["charged_cny"], "actual_model_calls": manifest["totals"]["actual_model_calls"],
            "formal_eligible": False,
            "verification_scope": "本地事件链/blob/校验器/注册表/账本重算；不验证外部网络真实性或语义质量。"}


def summarize_live_micro_pilot(repo_root=None, *, manifest_path=None, materials=None,
                               topic_id=TOPIC_ID) -> dict:
    """供看板使用的只读摘要；清单缺失或核验失败时返回不可用。"""
    repo = Path(repo_root).resolve() if repo_root is not None else ROOT
    spec = topic_spec(topic_id)
    path = Path(manifest_path) if manifest_path is not None else repo / spec["manifest_relative"]
    empty = {"available": False, "reason": "manifest_absent", "path": path.as_posix(),
             "actual_model_calls": 0, "charged_cny": "0.00000000", "subjects": [], "comparison": []}
    if not path.is_file():
        return empty
    try:
        check = verify_topic_live_micro_pilot(topic_id, path, repo_root=repo, materials=materials)
    except (ValueError, KeyError, OSError, TypeError):
        return {**empty, "reason": "manifest_verification_failed"}
    if not check["passed"]:
        return {**empty, "reason": "manifest_not_passed", "problems": check["problems"]}
    m = _parse_json(path.read_bytes())
    subjects = []
    for subject in spec["subjects"]:
        speech = next(n for n in m["nodes"] if n["node_id"] == f"speech_{subject}_T1")
        mirror = m["mirrors"][subject]
        mirror_items = [{"section": s["name"], "text": it["text"], "user_quote": it.get("user_quote"),
                         "citations": it["citations"]} for s in mirror["sections"] for it in s["items"]]
        responses = {b["arm"]: b["responses"] for b in m["branches"] if b["subject"] == subject}
        opposing = {}
        for arm in ARMS:
            node_id = f"measure_{subject}_{arm}_T3"
            opposing[arm] = m["measurement_texts"].get(node_id, {}).get("opposing_view")
        subjects.append({"subject": subject,
                         "initial_side": "positive" if spec["stances"][subject] > 0 else "negative",
                         "speech": speech["accepted_text"], "speech_attempts": len(speech["attempts"]),
                         "mirror_items": mirror_items, "intervention_responses": responses,
                         "t3_opposing_view": opposing,
                         "t1_opposing_view": m["measurement_texts"].get(f"measure_{subject}_P_T1", {})
                         .get("opposing_view")})
    trace = []
    for node in m["nodes"]:
        trace.append({"node_id": node["node_id"], "kind": node["kind"], "subject": node.get("subject"),
                      "arm": node.get("arm"), "timepoint": node.get("timepoint"),
                      "attempts": len(node["attempts"]),
                      "rejected": sum(a["status"] == "rejected" for a in node["attempts"]),
                      "event_id": node.get("accepted_event_id"),
                      "response_sha256": node["accepted_response_sha256"],
                      "visible_cards": len(node.get("visible_card_ids") or [])})
    return {"available": True, "reason": "verified", "path": path.as_posix(),
            "topic_id": topic_id, "topic_label": spec["label"], "subject_ids": list(spec["subjects"]),
            "manifest_sha256": m["manifest_sha256"], "run_id": m["run_id"], "design": m["design"],
            "actual_model_calls": m["totals"]["actual_model_calls"],
            "rejected_attempts": m["totals"]["rejected_attempts"],
            "charged_cny": m["totals"]["charged_cny"], "usage": m["totals"]["usage"],
            "billing_after_total_cny": m["totals"]["billing_after_total_cny"],
            "comparison": m["comparison"], "subjects": subjects, "trace": trace,
            "collisions": m["collisions"], "honest_gates": m["honest_gates"],
            "registry_sha256": m["registry_sha256"], "panel_sha256": m["panel_sha256"],
            "superseded_runs": m.get("superseded_runs", []), "step_spend": m.get("step_spend"),
            "billing_after_reserved_cny": m["totals"].get("billing_after_reserved_cny")}


# ------------------------------------------------------------------ 三议题矩阵（步骤41）
def _mean(values):
    values = [v for v in values if v is not None]
    return None if not values else sum(values) / len(values)


def _matrix_body(repo, materials_by_topic=None):
    """只由三份已核验清单 + 账本逐运行重算；不含随时间变化的账本全局快照。"""
    topics, rows = [], _ledger_rows(repo)
    descriptive = []
    for topic_id in TOPIC_IDS:
        spec = topic_spec(topic_id)
        path = Path(repo) / spec["manifest_relative"]
        _need(path.is_file(), "matrix_topic_manifest_missing:" + topic_id)
        check = verify_topic_live_micro_pilot(topic_id, path, repo_root=repo,
                                              materials=(materials_by_topic or {}).get(topic_id))
        _need(check["passed"] is True, "matrix_topic_not_verified:" + topic_id)
        m = _parse_json(path.read_bytes())
        kinds = {}
        for node in m["nodes"]:
            kinds[node["kind"]] = kinds.get(node["kind"], 0) + 1
        ledger = _ledger_totals(rows, m["run_id"])
        spend = m.get("step_spend") or {"final_run_charged_cny": ledger["charged_cny"],
                                         "superseded_charged_cny": "0.00000000",
                                         "step_charged_cny": ledger["charged_cny"]}
        topics.append({
            "topic_id": topic_id, "topic_label": spec["label"], "topic": m["user_views"] and None,
            "manifest_path": spec["manifest_relative"],
            "manifest_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "manifest_sha256": m["manifest_sha256"], "run_id": m["run_id"],
            "block_seed": spec["block_seed"], "subjects": list(spec["subjects"]),
            "subject_stances": spec["stances"],
            "final_run_model_calls": m["totals"]["actual_model_calls"],
            "rejected_attempts": m["totals"]["rejected_attempts"],
            "ledger_settles": ledger["settle_count"], "charged_cny": ledger["charged_cny"],
            "usage": ledger["usage"], "superseded_runs": len(m.get("superseded_runs") or []),
            "superseded_charged_cny": spend["superseded_charged_cny"],
            "topic_step_charged_cny": spend["step_charged_cny"],
            "node_kinds": dict(sorted(kinds.items())),
            "panel_records": len(m["panel_records"]), "comparison_rows": len(m["comparison"]),
            "panel_sha256": m["panel_sha256"], "registry_sha256": m["registry_sha256"],
            "four_stage_calls": sum(a.get("actual_model_calls", 0) for a in m["four_stage"]["attempts"]),
            "four_stage_failed_tasks": sum(1 for a in m["four_stage"]["attempts"] if a.get("status") != "succeeded"),
            # 采纳调用 = 48 个已接受节点 + 成功四阶段任务的调用；其余为被拒/重做的真实调用（均已记账）。
            "accepted_calls": sum(1 for n in m["nodes"] if n["accepted"]) + sum(
                a.get("actual_model_calls", 0) for a in m["four_stage"]["attempts"] if a.get("status") == "succeeded"),
        })
        topics[-1]["extra_calls"] = topics[-1]["final_run_model_calls"] - topics[-1]["accepted_calls"]
        topics[-1].pop("topic")
        for arm in ARMS:
            cells = {tp: [r for r in m["comparison"] if r["arm"] == arm and r["timepoint"] == tp]
                     for tp in ("T0", "T1", "T2", "T3")}
            row = {"topic_id": topic_id, "arm": arm, "n_subjects": len(cells["T3"])}
            for metric in ("BS", "Acc", "S", "O", "U"):
                for tp in ("T0", "T1", "T2", "T3"):
                    row[f"{metric}_{tp}_mean"] = _mean([r[metric] for r in cells[tp]])
            row["delta_BS_T1_T3_mean"] = (None if row["BS_T3_mean"] is None or row["BS_T1_mean"] is None
                                          else row["BS_T3_mean"] - row["BS_T1_mean"])
            descriptive.append(row)
    totals = {
        "topics": len(topics), "subjects": 2 * len(topics), "branches": 6 * len(topics),
        "final_run_model_calls": sum(t["final_run_model_calls"] for t in topics),
        "rejected_attempts": sum(t["rejected_attempts"] for t in topics),
        "accepted_calls": sum(t["accepted_calls"] for t in topics),
        "extra_calls_rejected_or_redone": sum(t["extra_calls"] for t in topics),
        "four_stage_failed_tasks": sum(t["four_stage_failed_tasks"] for t in topics),
        "panel_records": sum(t["panel_records"] for t in topics),
        "comparison_rows": sum(t["comparison_rows"] for t in topics),
        "blind_evaluations": sum(t["node_kinds"].get("blind_evaluation", 0) for t in topics),
        "subject_measurements": sum(t["node_kinds"].get("subject_measurement", 0) for t in topics),
        "mirrors": sum(t["node_kinds"].get("mirror", 0) for t in topics),
        "intervention_responses": sum(t["node_kinds"].get("intervention_response", 0) for t in topics),
        "routine_speeches": sum(t["node_kinds"].get("routine_speech", 0) for t in topics),
        "final_runs_charged_cny": "%.8f" % sum(float(t["charged_cny"]) for t in topics),
        "superseded_charged_cny": "%.8f" % sum(float(t["superseded_charged_cny"]) for t in topics),
        "all_live_micro_pilot_charged_cny": "%.8f" % sum(float(t["topic_step_charged_cny"]) for t in topics),
        "usage": {k: sum(t["usage"][k] for t in topics) for k in ("prompt_tokens", "completion_tokens",
                                                                   "total_tokens")},
    }
    return {"schema_version": SCHEMA_VERSION, "contract": MATRIX_CONTRACT,
            "subject_rule": SUBJECT_RULE, "topics": topics, "totals": totals,
            "descriptive_by_topic_arm": descriptive,
            "honest_gates": {"is_demo": False, "subject_is_language_model": True, "real_human_subjects": 0,
                             "statistical_inference": "none_descriptive_only_n2_per_topic",
                             "human_reviews_completed": 0, "full_I4_eligible": False,
                             "approved_for_experiment": False, "formal_eligible": False}}


def build_three_topic_live_micro_pilot_matrix(*, repo_root=None, matrix_path=None) -> dict:
    """汇总 TOPIC_01/02/03 三份已核验清单为总矩阵（只读清单与账本，不发请求）。"""
    repo = Path(repo_root).resolve() if repo_root is not None else ROOT
    body = _matrix_body(repo)
    ledger = _ledger(_load_config(repo), repo).status()
    matrix = {**body, "created_at_utc": utc_now(),
              "ledger_snapshot_at_build": {k: ledger.get(k) for k in (
                  "limit_cny", "initial_limit_cny", "limit_raise_count", "charged_cny", "reserved_cny",
                  "available_cny", "needs_reconciliation", "held_interrupted_request_count")}}
    matrix["matrix_sha256"] = canonical_hash(matrix)
    target = Path(matrix_path) if matrix_path is not None else repo / MATRIX_RELATIVE
    _need(not target.exists(), "matrix_exists")
    write_json_new(target, matrix)
    return {"status": "written", "path": target.as_posix(), "matrix_sha256": matrix["matrix_sha256"],
            "totals": matrix["totals"]}


def verify_three_topic_live_micro_pilot_matrix(matrix_path=None, *, repo_root=None,
                                               materials_by_topic=None) -> dict:
    """离线重验：三议题逐份重放核验 + 矩阵全部汇总值重算一致 + 哈希自洽。"""
    repo = Path(repo_root).resolve() if repo_root is not None else ROOT
    path = Path(matrix_path) if matrix_path is not None else repo / MATRIX_RELATIVE
    _need(path.is_file(), "matrix_missing")
    matrix = _parse_json(path.read_bytes())
    _need(canonical_hash({k: v for k, v in matrix.items() if k != "matrix_sha256"})
          == matrix["matrix_sha256"], "matrix_hash_mismatch")
    _need(matrix["contract"] == MATRIX_CONTRACT, "matrix_contract_invalid")
    problems = []
    try:
        body = _matrix_body(repo, materials_by_topic)
    except ValueError as exc:
        return {"contract": MATRIX_CONTRACT, "passed": False, "problems": [str(exc)[:160]],
                "matrix_sha256": matrix["matrix_sha256"]}
    for key, value in body.items():
        if matrix.get(key) != value:
            problems.append("matrix_mismatch:" + key)
    gates = matrix["honest_gates"]
    if gates.get("formal_eligible") is not False or gates.get("statistical_inference") != \
            "none_descriptive_only_n2_per_topic":
        problems.append("matrix_honest_gate")
    return {"contract": MATRIX_CONTRACT, "passed": not problems, "problems": problems,
            "matrix_sha256": matrix["matrix_sha256"], "totals": matrix["totals"],
            "verification_scope": "三议题清单逐份离线重放 + 汇总重算；不验证外部网络真实性或语义质量。"}


def summarize_three_topic_matrix(repo_root=None, *, matrix_path=None) -> dict:
    """供看板使用：矩阵核验通过才可用，并附三议题单项摘要。"""
    repo = Path(repo_root).resolve() if repo_root is not None else ROOT
    path = Path(matrix_path) if matrix_path is not None else repo / MATRIX_RELATIVE
    empty = {"available": False, "reason": "matrix_absent", "path": path.as_posix(), "topics": []}
    if not path.is_file():
        return empty
    try:
        cache = {t: load_materials(topic_id=t) for t in TOPIC_IDS}
        check = verify_three_topic_live_micro_pilot_matrix(path, repo_root=repo, materials_by_topic=cache)
    except (ValueError, KeyError, OSError, TypeError):
        return {**empty, "reason": "matrix_verification_failed"}
    if not check["passed"]:
        return {**empty, "reason": "matrix_not_passed", "problems": check["problems"]}
    matrix = _parse_json(path.read_bytes())
    summaries = [summarize_live_micro_pilot(repo, topic_id=t, materials=cache[t]) for t in TOPIC_IDS]
    _need(all(s["available"] for s in summaries), "matrix_topic_summary_unavailable")
    return {"available": True, "reason": "verified", "path": path.as_posix(),
            "matrix_sha256": matrix["matrix_sha256"], "totals": matrix["totals"],
            "topics": matrix["topics"], "descriptive_by_topic_arm": matrix["descriptive_by_topic_arm"],
            "ledger_snapshot_at_build": matrix["ledger_snapshot_at_build"],
            "honest_gates": matrix["honest_gates"], "subject_rule": matrix["subject_rule"],
            "topic_summaries": summaries}


def _load_config(repo):
    from .configuration import load_config
    return load_config(Path(repo) / "configs/core.json")
