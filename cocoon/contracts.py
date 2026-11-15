"""冻结证据包与阶段输出的离线契约；结构通过不代表语义支持。"""

from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
import math
import re
from urllib.parse import parse_qsl, unquote, urlsplit


STAGE_SECTIONS = {
    "search": ("evidence_overview", "coverage_gaps"),
    "label": ("source_assessment",),
    "blindspot": ("missing_dimensions", "possible_biases", "counterpoints"),
    "integrate": ("consensus", "disagreements", "blindspots", "sources"),
    "mirror": ("stance_analysis", "possible_biases", "blindspot_alert", "suggestions"),
}

_PACKET_FIELDS = (
    "schema_version", "packet_id", "topic_id", "topic", "stance_axis", "evidence", "is_demo",
)
_AXIS_FIELDS = ("name", "negative_label", "positive_label")
_EVIDENCE_FIELDS = (
    "evidence_id", "text", "source_url", "source_family_id", "content_sha256",
)
_OUTPUT_FIELDS = ("schema_version", "stage", "packet_id", "sections")
_SECTION_FIELDS = ("name", "items")
_ITEM_FIELDS = ("text", "citations", "epistemic_status")
_STATUSES = ("evidence_based", "uncertainty", "suggestion")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_HTTP_URL = re.compile(r"https?://", re.IGNORECASE)
_URL_DELIMITERS = frozenset("<>\"'`，。；、！？（）【】")
_SENSITIVE_QUERY_KEYS = frozenset((
    "api_key", "token", "password", "secret", "access_token", "key",
))


class _PlainTextParser(HTMLParser):
    def _reject(self, *args):
        raise ValueError("正文不允许 HTML 标记")

    handle_starttag = _reject
    handle_startendtag = _reject
    handle_endtag = _reject
    handle_comment = _reject
    handle_decl = _reject
    handle_pi = _reject
    unknown_decl = _reject


def _text(value, field, maximum=None, minimum=1):
    if type(value) is not str or not value.strip() or len(value) < minimum:
        raise ValueError(f"{field} 必须是非空字符串，至少 {minimum} 个字符")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{field} 超过字符上限 {maximum}")
    if "\x00" in value:
        raise ValueError(f"{field} 不允许 NULL 字符")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} 必须是有效 Unicode 文本") from exc
    return value


def _plain_text(value, field, maximum=None, minimum=1):
    _text(value, field, maximum, minimum)
    parser = _PlainTextParser(convert_charrefs=False)
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise ValueError(f"{field} 必须是无 HTML 标记的纯文本") from exc
    return value


def _fields(value, required, field, optional=()):
    if type(value) is not dict:
        raise ValueError(f"{field} 必须是对象")
    if any(type(key) is not str for key in value):
        raise ValueError(f"{field} 的字段名必须是字符串")
    actual = set(value)
    if not set(required) <= actual or actual - set(required) - set(optional):
        raise ValueError(f"{field} 字段必须符合白名单")


def _identifier(value, field):
    _text(value, field)
    if _ID.fullmatch(value) is None:
        raise ValueError(f"{field} 不符合 ID 格式")


def _check_query(query):
    # 只检查解码后的完整键名，不扫描查询值或普通正文。
    for key, _ in parse_qsl(query, keep_blank_values=True):
        while True:
            decoded = unquote(key)
            if decoded == key:
                break
            key = decoded
        if key.casefold() in _SENSITIVE_QUERY_KEYS:
            raise ValueError("source_url 查询参数不允许敏感键")


def _source_url(value, is_demo):
    _plain_text(value, "source_url")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("source_url 不允许空白、控制字符或反斜杠")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("source_url 格式无效") from exc
    if not hostname or parts.username is not None or parts.password is not None or "#" in value:
        raise ValueError("source_url 必须有 hostname，且不得含用户凭据或 fragment")
    if any(char in hostname for char in "%<>{}|\"'"):
        raise ValueError("source_url hostname 无效")
    if parts.scheme == "https":
        pass
    elif (
        is_demo and parts.scheme == "synthetic" and parts.netloc == "fixture"
        and parts.path.startswith("/") and len(parts.path) > 1 and port is None
    ):
        pass
    else:
        raise ValueError("source_url 仅允许 HTTPS；演示包额外允许 synthetic://fixture/ 路径")
    _check_query(parts.query)


def validate_packet(packet: dict, allow_demo=False) -> dict:
    """校验严格白名单与完整性，返回不共享可变子对象的证据包。"""
    if type(allow_demo) is not bool:
        raise ValueError("allow_demo 必须是布尔值")
    _fields(packet, _PACKET_FIELDS, "packet")
    if type(packet["schema_version"]) is not str or packet["schema_version"] != "1.0":
        raise ValueError("packet.schema_version 必须是 1.0")
    if type(packet["is_demo"]) is not bool:
        raise ValueError("packet.is_demo 必须是布尔值")
    if packet["is_demo"] and not allow_demo:
        raise ValueError("演示包需要显式 allow_demo=True")
    _identifier(packet["packet_id"], "packet_id")
    _identifier(packet["topic_id"], "topic_id")
    _plain_text(packet["topic"], "topic", 2000)
    axis = packet["stance_axis"]
    _fields(axis, _AXIS_FIELDS, "stance_axis")
    for name in _AXIS_FIELDS:
        _plain_text(axis[name], f"stance_axis.{name}")
    evidence = packet["evidence"]
    if type(evidence) is not list or not 1 <= len(evidence) <= 100:
        raise ValueError("evidence 必须是包含 1 至 100 项的列表")
    total_characters = 0
    evidence_ids = set()
    for entry in evidence:
        _fields(entry, _EVIDENCE_FIELDS, "evidence 项")
        _identifier(entry["evidence_id"], "evidence_id")
        if entry["evidence_id"] in evidence_ids:
            raise ValueError("evidence_id 不允许在 evidence 列表内重复")
        evidence_ids.add(entry["evidence_id"])
        _identifier(entry["source_family_id"], "source_family_id")
        text = _plain_text(entry["text"], "evidence.text", 20000)
        total_characters += len(text)
        if total_characters > 200000:
            raise ValueError("evidence 正文总体字符数不能超过 200000")
        _source_url(entry["source_url"], packet["is_demo"])
        digest = _text(entry["content_sha256"], "content_sha256")
        if digest != hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise ValueError("content_sha256 与原始正文不一致")
    return deepcopy(packet)


def _stage(stage):
    if type(stage) is not str or stage not in STAGE_SECTIONS:
        raise ValueError("未知 stage")
    return STAGE_SECTIONS[stage]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 不允许重复键")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("JSON 不允许 NaN 或 Infinity")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON 不允许非有限数值")
    return number


def _known_links(text, source_urls):
    matched_end = 0
    # 优先完整匹配较长冻结 URL，避免把其路径标点或查询内的 URL 拆开。
    urls = sorted(source_urls, key=len, reverse=True)
    for match in _HTTP_URL.finditer(text):
        if match.start() < matched_end:
            continue
        for url in urls:
            end = match.start() + len(url)
            if text.startswith(url, match.start()) and (
                end == len(text) or text[end].isspace() or text[end] in _URL_DELIMITERS
            ):
                matched_end = end
                break
        else:
            raise ValueError("输出不得引入冻结 source_url 集合之外的 HTTP(S) URL")


def parse_stage_output(raw_text: str, stage: str, packet: dict, user_view: str | None = None) -> dict:
    """只解析原始严格 JSON；不修复模型输出，也不判断引用的语义支持。"""
    section_names = _stage(stage)
    # 此入口接受已标记的演示包；正式/演示授权由 runner 调用 validate_packet 控制。
    frozen = validate_packet(packet, allow_demo=True)
    _text(raw_text, "raw_text", 200000)
    if user_view is not None:
        _plain_text(user_view, "user_view", minimum=10 if stage == "mirror" else 1)
    if stage == "mirror" and user_view is None:
        raise ValueError("mirror 需要至少 10 个字符的非空 user_view")
    try:
        parsed = json.loads(
            raw_text, object_pairs_hook=_unique_object,
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except (ValueError, RecursionError) as exc:
        raise ValueError("raw_text 必须是无重复键和非有限数值的严格 JSON") from exc
    _fields(parsed, _OUTPUT_FIELDS, "output")
    if type(parsed["schema_version"]) is not str or parsed["schema_version"] != "1.0":
        raise ValueError("output.schema_version 必须是 1.0")
    if type(parsed["stage"]) is not str or parsed["stage"] != stage:
        raise ValueError("output.stage 必须与请求阶段一致")
    if type(parsed["packet_id"]) is not str or parsed["packet_id"] != frozen["packet_id"]:
        raise ValueError("output.packet_id 必须与冻结包一致")
    sections = parsed["sections"]
    if type(sections) is not list or len(sections) != len(section_names):
        raise ValueError("sections 必须是完整的阶段章节列表")
    evidence_ids = {entry["evidence_id"] for entry in frozen["evidence"]}
    source_urls = {entry["source_url"] for entry in frozen["evidence"]}
    cited_ids = set()
    total_items = 0
    for section, expected_name in zip(sections, section_names):
        _fields(section, _SECTION_FIELDS, "section")
        if type(section["name"]) is not str or section["name"] != expected_name:
            raise ValueError("章节必须按固定顺序完整出现且不重复")
        items = section["items"]
        if type(items) is not list or len(items) > 30:
            raise ValueError("items 必须是最多 30 项的列表")
        total_items += len(items)
        for item in items:
            _fields(item, _ITEM_FIELDS, "item", ("user_quote",) if stage == "mirror" else ())
            text = _plain_text(item["text"], "item.text", 8000)
            _known_links(text, source_urls)
            status = item["epistemic_status"]
            if type(status) is not str or status not in _STATUSES:
                raise ValueError("epistemic_status 必须使用三个允许标签之一")
            citations = item["citations"]
            if type(citations) is not list:
                raise ValueError("citations 必须是列表")
            item_citations = set()
            for citation in citations:
                if type(citation) is not str or citation not in evidence_ids:
                    raise ValueError("citations 只能包含已知 evidence_id")
                if citation in item_citations:
                    raise ValueError("citations 不允许重复 ID")
                item_citations.add(citation)
            if status == "evidence_based" and not item_citations:
                raise ValueError("evidence_based 至少需要一个引用")
            if stage == "mirror":
                if status == "evidence_based" and "user_quote" not in item:
                    raise ValueError("mirror 的 evidence_based 项必须带 user_quote")
                if "user_quote" in item:
                    quote = _plain_text(item["user_quote"], "user_quote")
                    if quote not in user_view:
                        raise ValueError("user_quote 必须是 user_view 原文的精确非空子串")
                    _known_links(quote, source_urls)
            cited_ids.update(item_citations)
    if total_items == 0:
        raise ValueError("全部章节的总 items 数至少为 1")
    return {
        "output": deepcopy(parsed),
        "citation_audit": {
            "schema_valid": True,
            "known_evidence_only": True,
            "cited_ids": sorted(cited_ids),
            "semantic_entailment": "pending_independent_review",
            "formal_eligible": False,
        },
    }


def output_contract(stage: str) -> dict:
    """返回可直接序列化的稳定系统约束，不提供虚构结果示例。"""
    section_names = _stage(stage)
    mirror = stage == "mirror"
    return {
        "schema_version": "1.0",
        "stage": stage,
        "section_names": list(section_names),
        "required_output_fields": list(_OUTPUT_FIELDS),
        "required_section_fields": list(_SECTION_FIELDS),
        "required_item_fields": list(_ITEM_FIELDS),
        "allowed_item_fields": list(_ITEM_FIELDS) + (["user_quote"] if mirror else []),
        "epistemic_statuses": list(_STATUSES),
        "additional_fields_allowed": False,
        "serialization": "仅输出一个严格 JSON 对象，不要代码围栏、前后说明、重复键、NaN、Infinity 或 null。不得修复或截取响应。",
        "packet_id_rule": "必须逐字等于输入冻结证据包的 packet_id。",
        "section_order": "按 section_names 顺序完整输出每节，不得缺失、增补或重复；每节仅含 name 和 items。",
        "limits": {
            "raw_text_max_characters": 200000,
            "item_text_max_characters": 8000,
            "items_per_section_max": 30,
            "total_items_min": 1,
            "empty_sections_allowed": True,
        },
        "text_rules": "text 和存在的 user_quote 必须是非空、有效 Unicode 纯文本，不得有 NULL 字符或 HTML 标记；允许 <2 等比较。",
        "source_url_rule": "正文与引文中出现的 HTTP(S) URL 必须逐字匹配冻结包的 source_url，不得引入新的路径、查询或 fragment。使用空白或中文标点分隔 URL；不裁剪 URL 尾部字符。",
        "citation_rules": {
            "known_evidence_ids_only": True,
            "duplicates_allowed": False,
            "evidence_based_min_citations": 1,
            "uncertainty_min_citations": 0,
            "suggestion_min_citations": 0,
        },
        "retrieval_rule": "仅使用冻结证据包，不得声称已经检索、联网搜索或独立核验；本接口不执行检索。",
        "user_quote_rules": {
            "allowed": mirror,
            "user_view_required": mirror,
            "user_view_min_characters": 10 if mirror else None,
            "required_for_evidence_based": mirror,
            "nonempty_exact_substring": True,
            "optional_for_other_statuses": mirror,
        },
        "input_authorization": "runner 须先调用 validate_packet；演示包仅可通过显式 allow_demo=True 授权，解析输出不提升资格。",
        "id_rule": "packet_id、topic_id、每项 evidence_id 和 source_family_id 格式均为 [A-Za-z][A-Za-z0-9_-]{0,63}；仅 evidence_id 要求在 evidence 列表内唯一，source_family_id 可以共享。",
        "semantic_review_rule": "结构校验不判断句子是否真的受到引用支持，也不验证自然语言中的检索声明；必须独立审查。",
        "semantic_entailment": "pending_independent_review",
        "formal_eligible": False,
    }
