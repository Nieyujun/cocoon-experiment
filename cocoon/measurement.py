"""纯离线测量契约；不读写文件、不调用模型、不建立正式真值或真实观测。

公共问卷与私有评分依据分开；盲评仅接收notes/opposing及私有量规，不看客观题。
所有validated封装均可从原数据重验；普通hash不是签名或身份/实际送达证明。
extract_t1_natural_view沿用任务指定API名，但内容是测量自报，不是原方案常规发言。
"""
from copy import deepcopy
from functools import wraps
import hashlib
import html
import json
import math
import re
import unicodedata
from urllib.parse import unquote

from . import metrics, runtime, scheduling
from .contracts import _plain_text

__all__ = [
    "validate_public_questionnaire", "validate_private_gold_bundle",
    "prepare_subject_measurement_context", "parse_subject_measurement_output",
    "extract_t1_natural_view", "build_blind_evaluation_packet",
    "parse_blind_evaluation_output", "score_measurement_record",
]
MEASUREMENT_VERSION = "measurement_contract_v1"
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_POINTS = {"T0", "T1", "T2", "T3"}
_PARTS = ("summary", "main_evidence", "boundary")
_MAX_RAW = 200000
_MAX_TEXT = 4000
_MAX_NOTES = 30
_PRIVATE_METADATA = re.compile(
    r"(?:gold|truth|is_accurate|stance|answer|analysis|justification|rubric|dimensions|"
    r"mastered|recognized_gap|真值|真假|答案|金标准|标准答案|六维清单)", re.I)
_BLIND_LEAK = re.compile(
    r"(?<![a-z0-9])(?:t[0-3]|i[0-5]|s[0-9]+|env(?:ironment)?|world(?:_?id|[0-9]+)?|"
    r"block(?:_?id|[0-9]+)?|seed|agent(?:_?id)?|subject_?id|timepoint|questionnaire_?id|"
    r"experiment_?id|arm|homogeneous|balanced|treatment|control|is_demo|gold_answers|p_true|answers)"
    r"(?![a-z0-9])|实验组|对照组|干预组|世界编号|个体编号|分组标签|测量时点", re.I)
_SUBJECT_SYSTEM = (
    "仅依据你已有的可见材料回答当前公共问卷。锚题和迁移题均给出p_true，"
    "可省略无法作答的题目，不编造概率。返回严格JSON，只有answers、evidence_notes、opposing_view。"
    "answers每项含item_id、p_true、brief_reason、cited_card_ids，choice可省略；"
    "evidence_notes每项含known_evidence和remaining_question；"
    "opposing_view含summary、main_evidence、boundary。引用仅能使用给定visible_card_ids。"
    "不自评准确率、维度得分或异见理解得分。"
)


def _need(condition, code):
    if not condition:
        raise ValueError("measurement:" + code)


def _api(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ValueError as exc:
            if str(exc).startswith("measurement:"):
                raise
            raise ValueError("measurement:invalid_contract") from None
        except (TypeError, KeyError, IndexError, OverflowError, RecursionError, UnicodeError):
            raise ValueError("measurement:invalid_contract") from None
    return call


def _fields(value, required, optional=()):
    _need(type(value) is dict and all(type(k) is str for k in value)
          and set(required) <= set(value) <= set(required) | set(optional), "fields_not_allowed")
    runtime._reject_sensitive_keys(value)


def _text(value, minimum=1, maximum=_MAX_TEXT):
    _plain_text(value, "text", maximum=maximum, minimum=minimum)
    _need(not any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value), "text_control_character")
    # 不修复或解码原文；同时拒绝编码的HTML标记，避免下游显示解码改变语义。
    decoded = value
    for _ in range(4):
        newer = html.unescape(decoded)
        _plain_text(newer, "decoded_text", maximum=maximum, minimum=1)
        _need(not any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in newer), "encoded_control_character")
        if newer == decoded:
            break
        decoded = newer
    return value


def _normalized(value):
    text = unicodedata.normalize("NFKC", value)
    for _ in range(4):
        decoded = html.unescape(unquote(text))
        if decoded == text:
            break
        text = decoded
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)


def _id(value, blind=False):
    _need(type(value) is str and _ID.fullmatch(value) is not None, "identifier_invalid")
    normalized = _normalized(value)
    _need(scheduling._LABEL_TOKEN.search(normalized) is None
          and _PRIVATE_METADATA.search(normalized) is None, "identifier_label_leak")
    if blind:
        _need(_BLIND_LEAK.search(normalized) is None
              and re.search(r"env|world|block|seed|homogeneous|balanced|[TI][0-5]|S[0-9]{2}",
                            normalized, re.I) is None, "blind_identity_leak")
    return value


def _strict(raw):
    _need(type(raw) is str and bool(raw.strip()) and len(raw) <= _MAX_RAW, "raw_text_invalid")
    raw.encode("utf-8")
    value = runtime._parse_json(raw)
    runtime._reject_sensitive_keys(value)
    # _parse_json拒绝NaN字面量；再拒绝溢出指数如1e999成为inf。
    runtime.canonical_hash(value)
    return value


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return runtime.canonical_hash(value)


def _ids(values, maximum=10000):
    _need(type(values) is list and len(values) <= maximum, "id_list_invalid")
    for item in values:
        _id(item)
    _need(len(set(values)) == len(values), "duplicate_id")
    return deepcopy(values)


@_api
def validate_public_questionnaire(questionnaire) -> dict:
    """8锚题；可缺transfer_items或提供空列表/恰好4题。只允许公共字段。"""
    _fields(questionnaire, ("schema_version", "questionnaire_id", "topic_id", "anchor_items", "is_demo"),
            ("transfer_items",))
    _need(questionnaire["schema_version"] == "1.0" and type(questionnaire["is_demo"]) is bool,
          "questionnaire_version_or_demo_invalid")
    _id(questionnaire["questionnaire_id"])
    _id(questionnaire["topic_id"])
    anchors, transfers = questionnaire["anchor_items"], questionnaire.get("transfer_items", [])
    _need(type(anchors) is list and len(anchors) == 8, "anchor_count_must_be_eight")
    _need(type(transfers) is list and len(transfers) in (0, 4), "transfer_count_invalid")
    seen = set()
    for item in anchors + transfers:
        _fields(item, ("item_id", "statement"))
        _id(item["item_id"])
        _text(item["statement"])
        _need(item["item_id"] not in seen, "duplicate_question_item")
        seen.add(item["item_id"])
    result = deepcopy(questionnaire)
    result.setdefault("transfer_items", [])
    return result


def _validate_gold(gold, questionnaire):
    q = validate_public_questionnaire(questionnaire)
    _fields(gold, ("schema_version", "questionnaire_id", "topic_id", "is_demo",
                   "gold_answers", "dimensions", "opposing_rubric"))
    _need(gold["schema_version"] == "1.0" and type(gold["is_demo"]) is bool
          and gold["is_demo"] == q["is_demo"] and gold["questionnaire_id"] == q["questionnaire_id"]
          and gold["topic_id"] == q["topic_id"], "gold_questionnaire_mismatch")
    rows = gold["gold_answers"]
    expected = {i["item_id"]: group for name, group in (("anchor_items", "anchor"), ("transfer_items", "transfer"))
                for i in q[name]}
    _need(type(rows) is list and len(rows) == len(expected), "gold_coverage_mismatch")
    seen = set()
    for row in rows:
        _fields(row, ("item_id", "partition", "truth"))
        _id(row["item_id"])
        _need(row["item_id"] in expected and row["item_id"] not in seen
              and row["partition"] == expected[row["item_id"]], "gold_item_or_partition_invalid")
        _need(type(row["truth"]) is int and row["truth"] in (0, 1), "gold_truth_not_binary_int")
        seen.add(row["item_id"])
    _dimensions(gold["dimensions"])
    _rubric(gold["opposing_rubric"])
    return deepcopy(gold), q


def _dimensions(values):
    _need(type(values) is list and len(values) == 6, "dimension_count_must_be_six")
    seen = set()
    for item in values:
        _fields(item, ("dimension_id", "name", "criteria"))
        _id(item["dimension_id"], blind=True)
        _need(item["dimension_id"] not in seen, "duplicate_dimension")
        seen.add(item["dimension_id"])
        _text(item["name"])
        _text(item["criteria"])


def _rubric(value):
    _fields(value, _PARTS)
    for part in _PARTS:
        _fields(value[part], ("0", "1", "2"))
        for criterion in value[part].values():
            _text(criterion)


@_api
def validate_private_gold_bundle(gold_bundle, questionnaire) -> dict:
    """私有输入gold_answers=[{item_id,partition:'anchor'|'transfer',truth:int}]。

    返回评分侧专用绑定封装，包含公共问卷副本及原私有bundle；绝不可整体传给受试/助手。
    量规三个分项各为{'0':纯文本,'1':纯文本,'2':纯文本}。不赋予金标准真实性。
    """
    gold, q = _validate_gold(gold_bundle, questionnaire)
    return {"contract": "validated_private_gold_v1", "bundle": gold, "questionnaire": q,
            "questionnaire_sha256": _hash(q), "bundle_sha256": _hash(gold),
            "private_only": True, "formal_eligible": False}


def _gold_record(record):
    _fields(record, ("contract", "bundle", "questionnaire", "questionnaire_sha256", "bundle_sha256",
                     "private_only", "formal_eligible"))
    expected = validate_private_gold_bundle(record["bundle"], record["questionnaire"])
    _need(_hash(record) == _hash(expected), "gold_record_changed")
    return expected


def _public_history_message(message):
    _fields(message, ("role", "content"))
    _need(message["role"] in ("system", "user", "assistant"), "history_role_invalid")
    _text(message["content"], maximum=20000)
    # 历史文字中显式序列化的私有字段不能冒充普通发言。
    _need(re.search(r'(?:[\"\x27]?)(?:gold_answers|gold_bundle|dimensions|opposing_rubric|is_accurate|'
                    r'stance|standard_answer|标准答案|金标准)(?:[\"\x27]?)\s*[:=：]',
                    _normalized(message["content"]), re.I) is None, "private_history_metadata")


@_api
def prepare_subject_measurement_context(world_state, agent_id, questionnaire, *, allow_demo=False) -> dict:
    """显式状态适配：agents列表、messages[agent_id]、timepoint、is_demo必需。

    新增受控exposure_history列表记录 {event_id,event_type,agent_id,status,sent_to_model,cards}。
    仅exposure_delivered/status=delivered/sent_to_model=True属于实际已见；prepared_inputs、
    库存、其他个体或未送达记录不入白名单。cards是已有scheduling公共卡字段的完整列表。
    记录的真实性/事件签名由上层核验；本模块只核对传入声明，不读blob、不推定真实送达。
    返回messages才是待发送受试输入；外层hash/timepoint等仅控制器审计，不整体发送。
    """
    _need(type(world_state) is dict and type(allow_demo) is bool, "world_state_invalid")
    q = validate_public_questionnaire(questionnaire)
    _need(type(world_state.get("is_demo")) is bool and world_state["is_demo"] == q["is_demo"], "demo_mismatch")
    _need(not q["is_demo"] or allow_demo, "demo_requires_authorization")
    _id(agent_id)
    _need(type(world_state.get("timepoint")) is str and world_state["timepoint"] in _POINTS, "timepoint_required")
    _need(world_state.get("measurement_writes_to_memory", False) is False, "measurement_memory_write_forbidden")
    before = _hash(world_state)
    state = runtime.measurement_view(world_state)
    _need(type(state.get("agents")) is list, "agents_list_required")
    agents = state["agents"]
    _need(all(type(a) is dict and type(a.get("agent_id")) is str for a in agents), "agent_identity_invalid")
    ids = [a["agent_id"] for a in agents]
    _need(len(set(ids)) == len(ids) and ids.count(agent_id) == 1, "agent_missing_or_duplicate")
    _need(type(state.get("messages")) is dict and agent_id in state["messages"], "agent_messages_missing")
    history = state["messages"][agent_id]
    _need(type(history) is list and len(history) <= 1000, "history_messages_invalid")
    for message in history:
        _public_history_message(message)
    # 可复用显式的公共系统提示；不透传整个world或agent画像/私有记忆。
    history = deepcopy(history)
    if "system_prompt" in state:
        system_message = {"role": "system", "content": state["system_prompt"]}
        _public_history_message(system_message)
        history.insert(0, system_message)
    events = state.get("exposure_history", [])
    _need(type(events) is list and len(events) <= 10000, "exposure_history_invalid")
    seen_events, cards, event_ids = set(), {}, []
    for event in events:
        _fields(event, ("event_id", "event_type", "agent_id", "status", "sent_to_model", "cards"))
        _id(event["event_id"])
        _id(event["agent_id"])
        _need(event["event_id"] not in seen_events, "duplicate_exposure_event")
        seen_events.add(event["event_id"])
        _need(type(event["sent_to_model"]) is bool and event["event_type"] in ("exposure_delivered", "exposure_prepared")
              and event["status"] in ("delivered", "prepared", "failed", "cancelled"), "exposure_event_invalid")
        _need(type(event["cards"]) is list, "event_cards_invalid")
        delivered = event["event_type"] == "exposure_delivered" and event["status"] == "delivered" and event["sent_to_model"] is True
        _need(not (event["status"] == "delivered" and not delivered), "contradictory_delivery_claim")
        if event["agent_id"] != agent_id or not delivered:
            continue
        batch = set()
        for card in event["cards"]:
            _fields(card, scheduling._PUBLIC_FIELDS, ("is_demo",))
            public = scheduling.public_card(card)
            for key in ("card_id", "topic_id", "source_family_id"):
                _id(public[key])
            for key in ("source_url", "transformation_type"):
                _text(public[key])
                _need(scheduling._LABEL_TOKEN.search(_normalized(public[key])) is None
                      and _PRIVATE_METADATA.search(_normalized(public[key])) is None, "visible_card_metadata_leak")
            _public_history_message({"role": "user", "content": public["text"]})
            _need(public["topic_id"] == q["topic_id"], "card_topic_mismatch")
            _need(public.get("is_demo", q["is_demo"]) == q["is_demo"], "card_demo_mismatch")
            cid = public["card_id"]
            _need(cid not in batch, "duplicate_card_in_event")
            batch.add(cid)
            _need(cid not in cards or _hash(cards[cid]) == _hash(public), "visible_card_content_changed")
            cards[cid] = public
        event_ids.append(event["event_id"])
    visible = list(cards)
    payload = {"questionnaire": q, "visible_card_ids": visible}
    messages = deepcopy(history) + [{"role": "system", "content": _SUBJECT_SYSTEM},
                                    {"role": "user", "content": _dump(payload)}]
    _need(sum(len(m["content"]) for m in messages) <= 250000, "subject_context_too_large")
    _need(_hash(world_state) == before, "measurement_mutated_world")
    return {"messages": messages, "visible_card_ids": visible, "timepoint": state["timepoint"],
            "questionnaire_sha256": _hash(q), "source_exposure_event_ids": event_ids,
            "measurement_writes_to_memory": False, "world_state_before_sha256": before,
            "world_state_after_sha256": _hash(world_state), "is_demo": q["is_demo"],
            "delivery_claim_verified_externally": False, "formal_eligible": False}


def _subject_output(value, q, visible):
    _fields(value, ("answers", "evidence_notes", "opposing_view"))
    items = {i["item_id"] for name in ("anchor_items", "transfer_items") for i in q[name]}
    answers = value["answers"]
    _need(type(answers) is list and len(answers) <= len(items), "answers_count_invalid")
    seen = set()
    for answer in answers:
        _fields(answer, ("item_id", "p_true", "brief_reason", "cited_card_ids"), ("choice",))
        _id(answer["item_id"])
        _need(answer["item_id"] in items and answer["item_id"] not in seen, "answer_item_unknown_or_duplicate")
        seen.add(answer["item_id"])
        probability = answer["p_true"]
        _need(type(probability) in (int, float) and math.isfinite(probability) and 0 <= probability <= 1,
              "probability_invalid")
        if "choice" in answer:
            _need(type(answer["choice"]) in (bool, int) and answer["choice"] in (0, 1), "choice_invalid")
        _text(answer["brief_reason"])
        cites = _ids(answer["cited_card_ids"])
        _need(set(cites) <= set(visible), "cited_unseen_card")
    _notes_and_opposing(value)


def _notes_and_opposing(value):
    notes = value["evidence_notes"]
    _need(type(notes) is list and 1 <= len(notes) <= _MAX_NOTES, "evidence_notes_count_invalid")
    for note in notes:
        _fields(note, ("known_evidence", "remaining_question"))
        _text(note["known_evidence"])
        _text(note["remaining_question"])
    _fields(value["opposing_view"], _PARTS)
    for part in _PARTS:
        _text(value["opposing_view"][part])


@_api
def parse_subject_measurement_output(raw_text, questionnaire, *, visible_card_ids, timepoint=None) -> dict:
    """严格附录B三字段原输出；缺题不补。timepoint须由上层显式提供，不从ID推断。

    返回控制器专用封装，保留原JSON、公共问卷、已见ID和校验摘要以供后续重验；
    不得把完整封装当盲评输入或助手user_view。timepoint=None可计分，桥接须为T1。
    """
    q = validate_public_questionnaire(questionnaire)
    visible = _ids(visible_card_ids)
    _need(timepoint is None or (type(timepoint) is str and timepoint in _POINTS), "timepoint_invalid")
    output = _strict(raw_text)
    _subject_output(output, q, visible)
    return {"contract": "validated_measurement_v1", "output": deepcopy(output), "raw_text": raw_text,
            "output_sha256": _hash(output), "questionnaire": q, "questionnaire_sha256": _hash(q),
            "visible_card_ids": visible, "timepoint": timepoint, "is_demo": q["is_demo"],
            "source_event_ids": [], "actual_observation_verified": False, "formal_eligible": False}


def _measurement(record):
    _fields(record, ("contract", "output", "raw_text", "output_sha256", "questionnaire", "questionnaire_sha256",
                     "visible_card_ids", "timepoint", "is_demo", "source_event_ids", "actual_observation_verified", "formal_eligible"))
    expected = parse_subject_measurement_output(record["raw_text"], record["questionnaire"],
                                               visible_card_ids=record["visible_card_ids"], timepoint=record["timepoint"])
    _need(_hash(expected) == _hash(record), "measurement_record_changed")
    return expected


@_api
def extract_t1_natural_view(parsed_measurement, questionnaire) -> dict:
    """仅做T1测量自报的确定性拼装，不承认其为方案常规发言或真实T1观测。

    不拼入题目、item_id、p_true、choice、gold或实验条件，仅逐字保留自报理由与notes/
    opposing_view。顺序按公共问卷题序，区块用中文分号连接，不加入控制字符。
    metadata供控制器记录；真正发送mirror的只有user_view字符串。
    """
    record = _measurement(parsed_measurement)
    q = validate_public_questionnaire(questionnaire)
    _need(record["timepoint"] == "T1", "bridge_requires_T1")
    _need(_hash(q) == record["questionnaire_sha256"], "bridge_questionnaire_mismatch")
    data = record["output"]
    order = {i["item_id"]: n for n, i in enumerate(q["anchor_items"] + q["transfer_items"])}
    parts = ["测量中的自报判断理由："]
    for n, a in enumerate(sorted(data["answers"], key=lambda a: order[a["item_id"]]), 1):
        parts.append(f"理由{n}：" + a["brief_reason"])
    parts.append("已知与待查说明：")
    for n, note in enumerate(data["evidence_notes"], 1):
        parts.append(f"说明{n}已知：" + note["known_evidence"])
        parts.append(f"说明{n}待查：" + note["remaining_question"])
    for key, label in zip(_PARTS, ("另一观点", "其主要论据", "其适用边界")):
        parts.append(label + "：" + data["opposing_view"][key])
    view = "；".join(parts)
    _text(view, minimum=10, maximum=_MAX_RAW)
    original_texts = [a["brief_reason"] for a in data["answers"]]
    original_texts += [note[k] for note in data["evidence_notes"] for k in ("known_evidence", "remaining_question")]
    original_texts += list(data["opposing_view"].values())
    _need(all(s in view for s in original_texts), "bridge_lost_original_substring")
    return {"user_view": view, "user_view_sha256": hashlib.sha256(view.encode("utf-8")).hexdigest(),
            "content_origin": "subject_measurement_extracted", "source_output_sha256": record["output_sha256"],
            "is_demo": record["is_demo"], "source_event_ids": [], "actual_observation_verified": False,
            "natural_routine_speech": False, "requires_protocol_review": True,
            "mirror_contract_ready": True, "formal_eligible": False}


def _blind_scan(value):
    if type(value) is str:
        text = _normalized(value)
        _need(not any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in text), "blind_control_leak")
        _need(_BLIND_LEAK.search(text) is None, "blind_identity_leak")
    elif type(value) is dict:
        for key, item in value.items():
            _blind_scan(key)
            _blind_scan(item)
    elif type(value) is list:
        for item in value:
            _blind_scan(item)


def _blind_packet(packet):
    _fields(packet, ("blind_sample_id", "evidence_notes", "opposing_view", "dimensions", "opposing_rubric"))
    _id(packet["blind_sample_id"], blind=True)
    _notes_and_opposing(packet)
    _dimensions(packet["dimensions"])
    _rubric(packet["opposing_rubric"])
    _blind_scan(packet)
    _need(len(_dump(packet)) <= _MAX_RAW, "blind_packet_too_large")
    return deepcopy(packet)


@_api
def build_blind_evaluation_packet(parsed_measurement, gold_bundle, *, blind_sample_id) -> dict:
    """只返回实际发给评估器的五字段包；严格拒绝泄露，不删除或改写原文来脱敏。

    gold_bundle必须是validate_private_gold_bundle返回的完整私有封装。subject的answers/
    probabilities、gold_answers、questionnaire、timepoint和is_demo均不发给主观盲评器。
    盲评标识及全部发送文字只做显式泄露标记检查，不保证消除所有语义推断线索。
    """
    record, private = _measurement(parsed_measurement), _gold_record(gold_bundle)
    _need(record["questionnaire_sha256"] == private["questionnaire_sha256"], "blind_questionnaire_mismatch")
    output, gold = record["output"], private["bundle"]
    return _blind_packet({"blind_sample_id": blind_sample_id, "evidence_notes": output["evidence_notes"],
                          "opposing_view": output["opposing_view"], "dimensions": gold["dimensions"],
                          "opposing_rubric": gold["opposing_rubric"]})


@_api
def parse_blind_evaluation_output(raw_text, blind_packet) -> dict:
    packet = _blind_packet(blind_packet)
    output = _strict(raw_text)
    _fields(output, ("blind_sample_id", "dimensions", "opposing_scores"))
    _need(output["blind_sample_id"] == packet["blind_sample_id"], "blind_sample_id_mismatch")
    dims = output["dimensions"]
    _need(type(dims) is list and len(dims) == 6, "blind_dimension_count")
    notes = [n[k] for n in packet["evidence_notes"] for k in ("known_evidence", "remaining_question")]
    for dim, source in zip(dims, packet["dimensions"]):
        _fields(dim, ("dimension_id", "mastered", "recognized_gap", "evidence_quote"))
        _need(dim["dimension_id"] == source["dimension_id"], "blind_dimension_order")
        _need(type(dim["mastered"]) is bool and type(dim["recognized_gap"]) is bool, "blind_flags_not_bool")
        quote = dim["evidence_quote"]
        if quote is not None:
            _text(quote)
            _need(any(quote in n for n in notes), "blind_quote_not_subject_evidence")
        _need(not (dim["mastered"] or dim["recognized_gap"]) or quote is not None, "blind_quote_required")
    scores = output["opposing_scores"]
    _fields(scores, (*_PARTS, "rationale"))
    for name in _PARTS:
        _need(type(scores[name]) is int and 0 <= scores[name] <= 2, "opposing_score_not_int")
    _text(scores["rationale"])
    _blind_scan(output)
    return {"contract": "validated_blind_evaluation_v1", "output": deepcopy(output), "raw_text": raw_text,
            "blind_packet": packet, "blind_packet_sha256": _hash(packet), "output_sha256": _hash(output),
            "mastery_priority": "recognized_gap_counts_only_when_not_mastered", "formal_eligible": False}


def _blind_record(record):
    _fields(record, ("contract", "output", "raw_text", "blind_packet", "blind_packet_sha256", "output_sha256",
                     "mastery_priority", "formal_eligible"))
    expected = parse_blind_evaluation_output(record["raw_text"], record["blind_packet"])
    _need(_hash(expected) == _hash(record), "blind_record_changed")
    return expected


@_api
def score_measurement_record(parsed_measurement, gold_bundle, blind_evaluation=None) -> dict:
    """8锚题与4迁移题独立调用metrics；缺题不补概率，主BS分母永远为8。

    私有评分函数，不输出金标准，不回写受试。盲评须使用已校验封装并重建脱敏包一致性；
    两个主观维度旗标同时True时严格沿用metrics掌握优先。分数不代表正式实验资格。
    """
    record, private = _measurement(parsed_measurement), _gold_record(gold_bundle)
    _need(record["questionnaire_sha256"] == private["questionnaire_sha256"], "scoring_questionnaire_mismatch")
    gold, output = private["bundle"], record["output"]
    objective = {}
    for group in ("anchor", "transfer"):
        answers = {row["item_id"]: row["truth"] for row in gold["gold_answers"] if row["partition"] == group}
        objective[group] = (metrics.score_answers([a for a in output["answers"] if a["item_id"] in answers], answers)
                            if answers else None)
    coverage, understanding = None, None
    if blind_evaluation is not None:
        evaluation = _blind_record(blind_evaluation)
        expected = build_blind_evaluation_packet(record, private,
                                                 blind_sample_id=evaluation["output"]["blind_sample_id"])
        _need(_hash(expected) == evaluation["blind_packet_sha256"], "blind_evaluation_other_sample")
        coverage = metrics.coverage_score(evaluation["output"]["dimensions"])
        understanding = metrics.understanding_score([evaluation["output"]["opposing_scores"][p] for p in _PARTS])
    return {"anchor": objective["anchor"], "transfer": objective["transfer"],
            "primary_endpoint_partition": "anchor_eight_only", "transfer_scope": "exploratory_only",
            "coverage": coverage, "O": None if coverage is None else coverage["O"], "U": understanding,
            "blind_evaluation_status": "pending" if blind_evaluation is None else "validated",
            "timepoint": record["timepoint"], "is_demo": record["is_demo"],
            "source_output_sha256": record["output_sha256"], "gold_bundle_sha256": private["bundle_sha256"],
            "measurement_writes_to_memory": False, "actual_observation_verified": False, "formal_eligible": False}


# 步骤31仅追加；前部八个API及测量自报备选通道保持原行为。
import random

__all__ += ["extract_t1_routine_speech_view", "create_blind_sample_registry", "reconcile_blind_evaluations"]
BLIND_REGISTRY_VERSION = "validated_blind_registry_v1"
_ROUTINE_FIELDS = ("speech_id", "event_id", "world_id", "agent_id", "topic_id", "phase", "round",
                   "logical_seq", "event_type", "role", "status", "generated", "text", "is_demo")
_CONTROLLER_METADATA = ("world_id", "agent_id", "environment", "arm", "timepoint", "block_id")
_REGISTRY_LIMIT = 4096
_MEASUREMENT_TEXT_KEYS = re.compile(
    r"p_true|brief_reason|cited_card_ids|evidence_notes|opposing_view|known_evidence|remaining_question|"
    r"questionnaire|measurement|问卷回答|测量回答|系统提示词|system_prompt", re.I)


def _private_identifier(value):
    # 条件元数据只在控制器使用，允许W_I4等有意义ID，但不接受路径/HTML或空标识。
    _need(type(value) is str and _ID.fullmatch(value) is not None, "private_identifier_invalid")
    return value


@_api
def extract_t1_routine_speech_view(world_state, agent_id, *, topic_id=None, max_entries=8, allow_demo=False) -> dict:
    """从T1快照的routine_speech_history提取常规生成发言；不读问卷或messages回退。

    新状态契约：timepoint='T1'，round=8，logical_seq为快照边界，world_id、is_demo、agents必需。
    发言条目严格使用_ROUTINE_FIELDS：phase='exposure'、round1..8、正整数logical_seq、
    event_type='routine_speech_generated'或'discussion_reply'、role='assistant'、
    status='completed'、generated=True。原文空白/测量/系统/私有标签一律拒绝。
    同一世界其他人/其他议题只做排除，不混进引用；目标的未生成或未来发言报错。
    按轮次/逻辑序升序选最近max_entries条并披露整条排除ID，不截断单条文字。
    仅认定来源契约是常规发言；事件真实性、完整实验资格仍须外部核验。
    """
    _need(type(world_state) is dict and type(allow_demo) is bool, "routine_world_invalid")
    _need(type(max_entries) is int and 1 <= max_entries <= 64, "routine_max_entries_invalid")
    _need(type(world_state.get("is_demo")) is bool, "routine_demo_flag_required")
    _need(not world_state["is_demo"] or allow_demo, "demo_requires_authorization")
    _need(world_state.get("timepoint") == "T1" and type(world_state.get("round")) is int
          and world_state["round"] == 8, "routine_requires_T1_exposure_snapshot")
    cutoff = world_state.get("logical_seq")
    _need(type(cutoff) is int and cutoff >= 0, "routine_snapshot_sequence_required")
    _private_identifier(world_state.get("world_id"))
    _private_identifier(agent_id)
    if topic_id is not None:
        _id(topic_id)
    before = _hash(world_state)
    state = runtime.measurement_view(world_state)
    agents = state.get("agents")
    _need(type(agents) is list and all(type(a) is dict and type(a.get("agent_id")) is str for a in agents),
          "routine_agents_invalid")
    agent_ids = [a["agent_id"] for a in agents]
    _need(len(agent_ids) == len(set(agent_ids)) and agent_ids.count(agent_id) == 1, "routine_agent_not_unique")
    history = state.get("routine_speech_history")
    _need(type(history) is list and 1 <= len(history) <= 10000, "routine_history_required")
    seen_speeches, seen_events, seen_sequences = set(), set(), set()
    candidates, excluded = [], []
    for row in history:
        _fields(row, _ROUTINE_FIELDS)
        for name in ("speech_id", "event_id", "topic_id"):
            _id(row[name])
        _private_identifier(row["agent_id"])
        _private_identifier(row["world_id"])
        _need(row["world_id"] == state["world_id"] and row["agent_id"] in agent_ids,
              "routine_world_or_agent_mismatch")
        _need(type(row["round"]) is int and 1 <= row["round"] <= 8
              and type(row["logical_seq"]) is int and row["logical_seq"] > 0, "routine_order_invalid")
        _need(row["logical_seq"] <= cutoff, "routine_after_snapshot")
        _need(type(row["is_demo"]) is bool and row["is_demo"] == state["is_demo"], "routine_demo_mismatch")
        _need(row["speech_id"] not in seen_speeches and row["event_id"] not in seen_events
              and row["logical_seq"] not in seen_sequences, "routine_duplicate_record")
        seen_speeches.add(row["speech_id"])
        seen_events.add(row["event_id"])
        seen_sequences.add(row["logical_seq"])
        if row["agent_id"] != agent_id or (topic_id is not None and row["topic_id"] != topic_id):
            excluded.append({"speech_id": row["speech_id"], "reason": "other_agent_or_topic"})
            continue
        _need(row["phase"] == "exposure"
              and row["event_type"] in ("routine_speech_generated", "discussion_reply")
              and row["role"] == "assistant" and row["status"] == "completed"
              and row["generated"] is True, "not_completed_routine_speech")
        text = _text(row["text"], maximum=20000)
        normalized = _normalized(text)
        _need(_PRIVATE_METADATA.search(normalized) is None and _MEASUREMENT_TEXT_KEYS.search(normalized) is None,
              "routine_measurement_or_private_text")
        _blind_scan(text)
        candidates.append(row)
    _need(bool(candidates), "no_eligible_routine_speech")
    if topic_id is None:
        topics = {c["topic_id"] for c in candidates}
        _need(len(topics) == 1, "routine_topic_ambiguous")
        topic_id = next(iter(topics))
    ordered = sorted(candidates, key=lambda c: (c["round"], c["logical_seq"], c["speech_id"]))
    _need(all(a["logical_seq"] < b["logical_seq"] for a, b in zip(ordered, ordered[1:])),
          "routine_round_sequence_conflict")
    selected = ordered[-max_entries:]
    excluded += [{"speech_id": c["speech_id"], "reason": "outside_most_recent_entry_limit"}
                 for c in ordered[:-max_entries]]
    view = "；".join(["曝光阶段常规发言："] + [f"发言{n}：" + c["text"] for n, c in enumerate(selected, 1)])
    _text(view, minimum=10, maximum=_MAX_RAW)
    _need(all(c["text"] in view for c in selected), "routine_original_text_lost")
    _need(_hash(world_state) == before, "routine_world_mutated")
    return {"contract": "t1_routine_speech_view_v1", "content_origin": "subject_routine_speech",
            "natural_routine_speech": True, "requires_protocol_review": False,
            "user_view": view, "user_view_sha256": hashlib.sha256(view.encode("utf-8")).hexdigest(),
            "speech_ids": [c["speech_id"] for c in selected], "rounds": [c["round"] for c in selected],
            "source_event_ids": [c["event_id"] for c in selected],
            "source_record_sha256": {c["speech_id"]: _hash(c) for c in selected},
            "topic_id": topic_id, "selected_entry_count": len(selected), "eligible_entry_count": len(candidates),
            "excluded_entries": excluded, "selection_rule": "latest_whole_entries_by_round_logical_sequence_v1",
            "writes_to_memory": False, "world_state_before_sha256": before, "world_state_after_sha256": _hash(world_state),
            "is_demo": state["is_demo"], "actual_observation_verified": False,
            "mirror_contract_ready": True, "formal_eligible": False}


def _registry_metadata(entry, measurement):
    metadata = {k: deepcopy(entry[k]) for k in _CONTROLLER_METADATA}
    for name in ("world_id", "agent_id", "environment"):
        _private_identifier(metadata[name])
    _need(metadata["arm"] is None or (type(metadata["arm"]) is str and metadata["arm"] in {f"I{i}" for i in range(6)}),
          "registry_arm_invalid")
    _need(type(metadata["timepoint"]) is str and metadata["timepoint"] in _POINTS
          and metadata["timepoint"] == measurement["timepoint"], "registry_timepoint_mismatch")
    block = metadata["block_id"]
    _need((type(block) is int and block >= 0) or (type(block) is str and _ID.fullmatch(block) is not None),
          "registry_block_invalid")
    return metadata


@_api
def create_blind_sample_registry(entries, *, registry_seed) -> dict:
    """创建控制器私有registry；唯一可发送的是public_blind_packets列表。

    每entry严格为 parsed_measurement、gold_bundle（步骤30已验证封装）及六个
    _CONTROLLER_METADATA字段。重复(world,agent,timepoint,topic,questionnaire)拒绝；
    同一world的environment/arm/block固定，演示/非演示不能混用。顺序按seed随机打乱，
    再依此分配BLIND_0001等中性ID；不保证每个种子的排列都与原序不同。
    保留私有source_entries以重建registry验证映射，绝不把该封装发给评估器。
    registry_sha256须由控制器在可信位置记录；自含hash不是数字签名或抗全体重写证明。
    """
    _need(type(entries) is list and 1 <= len(entries) <= _REGISTRY_LIMIT, "registry_entries_invalid")
    _need(type(registry_seed) is int and 0 <= registry_seed < 2 ** 64, "registry_seed_invalid")
    before = _hash(entries)
    checked, seen, worlds, demo_flags = [], set(), {}, set()
    for entry in entries:
        _fields(entry, ("parsed_measurement", "gold_bundle", *_CONTROLLER_METADATA))
        record = _measurement(entry["parsed_measurement"])
        private = _gold_record(entry["gold_bundle"])
        _need(record["questionnaire_sha256"] == private["questionnaire_sha256"], "registry_questionnaire_mismatch")
        metadata = _registry_metadata(entry, record)
        demo_flags.add(record["is_demo"])
        key = (metadata["world_id"], metadata["agent_id"], metadata["timepoint"],
               record["questionnaire"]["topic_id"], record["questionnaire"]["questionnaire_id"])
        _need(key not in seen, "registry_duplicate_sample")
        seen.add(key)
        world_metadata = {k: metadata[k] for k in ("environment", "arm", "block_id")}
        if metadata["world_id"] in worlds:
            _need(_hash(worlds[metadata["world_id"]]) == _hash(world_metadata), "registry_world_condition_conflict")
        worlds[metadata["world_id"]] = world_metadata
        checked.append({"parsed_measurement": record, "gold_bundle": private, **metadata})
    _need(len(demo_flags) == 1, "registry_demo_mix_forbidden")
    order = list(range(len(checked)))
    random.Random(registry_seed).shuffle(order)
    packets, mapping = [], {}
    for slot, source_index in enumerate(order, 1):
        entry = checked[source_index]
        record, private = entry["parsed_measurement"], entry["gold_bundle"]
        sample_id = f"BLIND_{slot:04d}"
        packet = build_blind_evaluation_packet(record, private, blind_sample_id=sample_id)
        packets.append(packet)
        mapping[sample_id] = {
            **{k: deepcopy(entry[k]) for k in _CONTROLLER_METADATA},
            "topic_id": record["questionnaire"]["topic_id"],
            "questionnaire_id": record["questionnaire"]["questionnaire_id"],
            "is_demo": record["is_demo"], "source_entry_index": source_index,
            "source_entry_sha256": _hash(entry), "measurement_output_sha256": record["output_sha256"],
            "measurement_record_sha256": _hash(record), "questionnaire_sha256": record["questionnaire_sha256"],
            "gold_bundle_sha256": private["bundle_sha256"], "gold_record_sha256": _hash(private),
            "blind_packet_sha256": _hash(packet),
        }
    _need(_hash(entries) == before, "registry_input_mutated")
    registry = {
        "contract": BLIND_REGISTRY_VERSION, "registry_seed": registry_seed, "entry_count": len(checked),
        "source_entries": deepcopy(checked), "source_entries_sha256": _hash(checked),
        "permutation": order, "public_blind_packets": packets, "public_packets_sha256": _hash(packets),
        "private_mapping": mapping, "private_mapping_sha256": _hash(mapping),
        "controller_private_only": True, "sendable_field": "public_blind_packets",
        "is_demo": next(iter(demo_flags)), "new_model_calls": 0, "formal_eligible": False,
        "actual_observation_verified": False,
        "integrity_scope": "local_hash_and_deterministic_reconstruction_not_external_authentication",
    }
    registry["registry_sha256"] = _hash(registry)
    return deepcopy(registry)


def _verified_registry(registry):
    _fields(registry, ("contract", "registry_seed", "entry_count", "source_entries", "source_entries_sha256",
                       "permutation", "public_blind_packets", "public_packets_sha256", "private_mapping", "private_mapping_sha256",
                       "controller_private_only", "sendable_field", "is_demo", "new_model_calls", "formal_eligible",
                       "actual_observation_verified", "integrity_scope", "registry_sha256"))
    _need(registry["contract"] == BLIND_REGISTRY_VERSION, "registry_contract_invalid")
    body = {k: v for k, v in registry.items() if k != "registry_sha256"}
    _need(type(registry["registry_sha256"]) is str and _hash(body) == registry["registry_sha256"], "registry_hash_mismatch")
    expected = create_blind_sample_registry(registry["source_entries"], registry_seed=registry["registry_seed"])
    _need(_hash(expected) == _hash(registry), "registry_reconstruction_mismatch")
    return expected


def _registry_gold_by_id(gold_bundle, sample_ids):
    _need(type(gold_bundle) is dict, "registry_gold_invalid")
    if gold_bundle.get("contract") == "validated_private_gold_v1":
        checked = _gold_record(gold_bundle)
        return {sid: checked for sid in sample_ids}
    # 跨问卷批次显式传blind_id->validated private gold的完整映射，绝不猜测题目集合。
    _need(set(gold_bundle) == set(sample_ids), "registry_gold_id_set_mismatch")
    return {sid: _gold_record(gold_bundle[sid]) for sid in sample_ids}


@_api
def reconcile_blind_evaluations(blind_registry, blind_evaluations, measurements_by_id, gold_bundle, *,
                                expected_registry_sha256=None) -> dict:
    """核验匿名包/测量/金标准精确关联并回填控制器私有长面板（不写文件）。

    measurements_by_id必须为全部匿名ID->步骤30已校验测量封装。gold_bundle可为
    共用的已校验私有金标准，或跨问卷时全部匿名ID->私有封装映射。
    blind_evaluations是0..N个已校验盲评封装列表；同ID重复即拒绝，缺失者O/U保持None。
    不回填原world，不删除未完成样本；含私有条件的panel_records只能供统计侧消费。
    状态与分数全重算，不接受客户端的分数、计数或盲包摘要替代原输入重验。
    可选expected_registry_sha256从可信控制器存储取值，用来阻止整包一致性重写；
    未提供则仅自含哈希与重建校验，不宣称已有外部可信锚。
    """
    if expected_registry_sha256 is not None:
        _need(type(expected_registry_sha256) is str
              and re.fullmatch(r"[0-9a-f]{64}", expected_registry_sha256) is not None
              and type(blind_registry) is dict and blind_registry.get("registry_sha256") == expected_registry_sha256,
              "registry_trusted_digest_mismatch")
    before = _hash({"registry": blind_registry, "evaluations": blind_evaluations,
                    "measurements": measurements_by_id, "gold": gold_bundle})
    registry = _verified_registry(blind_registry)
    mapping = registry["private_mapping"]
    ids = [packet["blind_sample_id"] for packet in registry["public_blind_packets"]]
    _need(type(measurements_by_id) is dict and set(measurements_by_id) == set(ids), "registry_measurement_id_set_mismatch")
    golds = _registry_gold_by_id(gold_bundle, ids)
    measurements = {}
    packets_by_id = {p["blind_sample_id"]: p for p in registry["public_blind_packets"]}
    for sid in ids:
        record = _measurement(measurements_by_id[sid])
        private = golds[sid]
        bound = mapping[sid]
        _need(_hash(record) == bound["measurement_record_sha256"]
              and record["output_sha256"] == bound["measurement_output_sha256"], "registry_measurement_mismatch")
        _need(_hash(private) == bound["gold_record_sha256"]
              and private["bundle_sha256"] == bound["gold_bundle_sha256"], "registry_gold_mismatch")
        _need(_hash(build_blind_evaluation_packet(record, private, blind_sample_id=sid)) == bound["blind_packet_sha256"],
              "registry_blind_packet_mismatch")
        measurements[sid] = record
    _need(type(blind_evaluations) is list and len(blind_evaluations) <= len(ids), "registry_evaluations_invalid")
    evaluations = {}
    for submitted in blind_evaluations:
        evaluation = _blind_record(submitted)
        sid = evaluation["output"]["blind_sample_id"]
        _need(sid in mapping and sid not in evaluations, "registry_evaluation_unknown_or_duplicate")
        _need(evaluation["blind_packet_sha256"] == mapping[sid]["blind_packet_sha256"]
              and _hash(evaluation["blind_packet"]) == _hash(packets_by_id[sid]), "registry_evaluation_cross_sample")
        evaluations[sid] = evaluation
    panel, pending_ids = [], []
    # 行序固定为registry匿名发送序；不由评分到达顺序改变关联或丢弃pending样本。
    for sid in ids:
        bound = mapping[sid]
        evaluation = evaluations.get(sid)
        scores = score_measurement_record(measurements[sid], golds[sid], evaluation)
        if evaluation is None:
            pending_ids.append(sid)
        panel.append({
            "blind_sample_id": sid, **{k: deepcopy(bound[k]) for k in _CONTROLLER_METADATA},
            "topic_id": bound["topic_id"], "questionnaire_id": bound["questionnaire_id"],
            "is_demo": bound["is_demo"], "BS": scores["anchor"]["BS"],
            "Acc": scores["anchor"]["Acc"], "S": scores["anchor"]["S"],
            "BS_lower": scores["anchor"]["BS_lower"], "BS_upper": scores["anchor"]["BS_upper"],
            "anchor": scores["anchor"], "transfer": scores["transfer"],
            "O": scores["O"], "U": scores["U"], "coverage": scores["coverage"],
            "blind_evaluation_status": scores["blind_evaluation_status"],
            "objective_complete": scores["anchor"]["complete"],
            "row_complete": scores["anchor"]["complete"] and evaluation is not None,
            "primary_endpoint_partition": "anchor_eight_only", "transfer_scope": "exploratory_only",
            "measurement_output_sha256": bound["measurement_output_sha256"],
            "measurement_record_sha256": bound["measurement_record_sha256"],
            "gold_bundle_sha256": bound["gold_bundle_sha256"], "blind_packet_sha256": bound["blind_packet_sha256"],
            "blind_evaluation_sha256": None if evaluation is None else _hash(evaluation),
            "controller_private_only": True, "actual_observation_verified": False, "formal_eligible": False,
        })
    _need(_hash({"registry": blind_registry, "evaluations": blind_evaluations,
                 "measurements": measurements_by_id, "gold": gold_bundle}) == before, "reconciliation_input_mutated")
    result = {
        "contract": "reconciled_blind_panel_v1", "registry_sha256": registry["registry_sha256"],
        "trusted_registry_digest_supplied": expected_registry_sha256 is not None,
        "panel_records": panel, "pending_blind_sample_ids": pending_ids,
        "expected_samples": len(ids), "completed_blind_evaluations": len(evaluations),
        "pending_blind_evaluations": len(pending_ids),
        "complete_objective_records": sum(p["objective_complete"] for p in panel),
        "complete_panel_records": sum(p["row_complete"] for p in panel),
        "status": "all_blind_evaluations_reconciled" if not pending_ids else "partial_blind_evaluations",
        "controller_private_only": True, "send_to_evaluator": False, "writes_to_memory": False,
        "new_model_calls": 0, "is_demo": registry["is_demo"], "formal_eligible": False,
        "analysis_scope": "individual_long_panel_not_network_aggregate_or_formal_effect_estimate",
    }
    result["panel_sha256"] = _hash(panel)
    return result
