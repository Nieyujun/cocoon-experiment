"""离线 source_id_inline_v1 剂量装配；不改证据、不补长、不发送、不自动批准。"""

from copy import deepcopy
from datetime import date
import hashlib
import math
import re

from .contracts import _identifier, _plain_text, _source_url, validate_packet
from .runtime import canonical_hash


RESPONSE_PROMPT = "请用一段简短文字回应上述材料；如不确定可直接说明。"
_LENGTH_BASIS = (
    "Python len includes all Unicode codepoints punctuation whitespace "
    "and every model-visible label"
)
DISPLAY_POLICY = {
    "version": "source_id_inline_v1",
    "evidence_template": "[{evidence_id} | {source_id} {publisher_short} {publication_date}]\n{text}",
    "entry_separator": "\n\n",
    "visible_template": "阅读材料\n{evidence_block}\n\n阅读说明\n{additional_text}\n\n{response_prompt}",
    "response_prompt": RESPONSE_PROMPT,
    "unknown_date": "日期未明",
    "publication_date_format": "YYYY-MM-DD",
    "publisher_short_max_chars": 18,
    "evidence_rule": "完整保留原始正文及空白，不裁剪、不规范化、不截断。",
    "source_map_rule": "完整URL和来源族仅保留在审计映射及审阅链接，不属于受试文本。",
    "review_links_audience": "仅审阅者；受试者不获得链接或额外浏览动作。",
    "extra_browsing_actions": False,
    "length_basis": _LENGTH_BASIS,
    "additional_content_rule": "所有新增受试可见内容都必须加入visible_text后重新计字。",
    "legacy_rule": "这是新的受试实际输入版本，不声称旧版含完整URL的输入也已缩短。",
    "text_rule": "有效Unicode纯文本；拒绝HTML、NULL和CR，保留LF和制表符。",
    "treatment_label_rule": "受试可见文本不得含I4或I5字样（不区分大小写），拒绝而非删改。",
    "response_opportunities": 1,
    "formal_eligible": False,
}
_POLICY_SHA256 = canonical_hash(DISPLAY_POLICY)
_SOURCE_FIELDS = (
    "source_id", "publisher_short", "publication_date", "source_url", "source_family_id",
)
_PROVENANCE_FIELDS = ("content_origin", "natural_view_used", "source_event_ids")
_CHECK_NAMES = (
    "evidence_equal", "source_equal", "dose_within_range", "length_matched",
    "additional_length_matched", "responses_equal",
)
_CHECK_FIELDS = _CHECK_NAMES + (
    "total_difference_ratio", "additional_difference_ratio", "assembly_valid",
    "length_basis", "semantic_review", "natural_view_available", "formal_eligible",
    "deviations", "integrity",
)
_INTEGRITY_FIELDS = (
    "packet", "source_map_sha256", "min_chars", "max_chars", "tolerance", "pair_sha256",
)
_SOURCE_ID = re.compile(r"[A-Za-z\u3400-\u4dbf\u4e00-\u9fff][A-Za-z0-9_\-\u3400-\u4dbf\u4e00-\u9fff]{0,63}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_HTML_PREFIX = re.compile(r"<\s*(?:/?[A-Za-z]|[!?])")
_ARM_LABEL = re.compile(r"I[45]", re.IGNORECASE)


def _exact(value, fields, name):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError(f"{name} 字段必须严格符合白名单")


def _text(value, name):
    _plain_text(value, name)
    if any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in value):
        raise ValueError(f"{name} 不允许NULL、CR或其他控制字符；允许LF和制表符")
    # 同时拒绝未闭合的标签；不把 <2 等比较表达式当作HTML，也不解码实体。
    if _HTML_PREFIX.search(value):
        raise ValueError(f"{name} 不允许HTML标记")
    return value


def _source_index(source_index):
    if type(source_index) is not dict:
        raise ValueError("source_index 必须是按source_id索引的对象")
    by_origin = {}
    for key, source in source_index.items():
        _exact(source, _SOURCE_FIELDS, "source")
        sid = _text(source["source_id"], "source_id")
        if type(key) is not str or key != sid or not _SOURCE_ID.fullmatch(sid):
            raise ValueError("source_id 必须与键一致，且为1至64字符的ASCII或中文标识符")
        publisher = _text(source["publisher_short"], "publisher_short")
        if len(publisher) > 18 or any(
            not (32 <= ord(char) <= 126 or "\u3400" <= char <= "\u4dbf"
                 or "\u4e00" <= char <= "\u9fff" or char in "·（）《》、，。")
            or char in "<>[]|" for char in publisher
        ):
            raise ValueError("publisher_short 必须为不超过18字符的ASCII或中文单行机构标签")
        publication_date = source["publication_date"]
        if publication_date is not None:
            if type(publication_date) is not str or not _DATE.fullmatch(publication_date):
                raise ValueError("publication_date 必须为YYYY-MM-DD或null")
            if date.fromisoformat(publication_date).isoformat() != publication_date:
                raise ValueError("publication_date 不是严格日期")
        _source_url(source["source_url"], is_demo=False)
        _identifier(source["source_family_id"], "source_family_id")
        origin = (source["source_url"], source["source_family_id"])
        if origin in by_origin:
            raise ValueError("同一source_url与source_family_id组合存在重复映射歧义")
        by_origin[origin] = deepcopy(source)
    return by_origin


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compact_evidence(packet: dict, source_index: dict) -> dict:
    """返回新显示版本与相关完整来源记录；正文哈希基于原始UTF-8字节。"""
    if (canonical_hash(DISPLAY_POLICY) != _POLICY_SHA256
            or RESPONSE_PROMPT != DISPLAY_POLICY["response_prompt"]):
        raise ValueError("显示策略常量已改变；须显式定义新版本，不能沿用冻结版本")
    frozen = validate_packet(packet)
    sources = _source_index(source_index)
    blocks, evidence_ids, source_map = [], [], []
    seen_sources = set()
    for entry in frozen["evidence"]:
        text = _text(entry["text"], "evidence.text")
        origin = (entry["source_url"], entry["source_family_id"])
        if origin not in sources:
            raise ValueError("每条证据必须有URL与来源族同时精确匹配的唯一来源")
        source = sources[origin]
        evidence_ids.append(entry["evidence_id"])
        if source["source_id"] not in seen_sources:
            source_map.append(deepcopy(source))
            seen_sources.add(source["source_id"])
        label_date = source["publication_date"] or DISPLAY_POLICY["unknown_date"]
        blocks.append(
            f"[{entry['evidence_id']} | {source['source_id']} "
            f"{source['publisher_short']} {label_date}]\n{text}"
        )
    block = "\n\n".join(blocks)
    return {
        "evidence_block": block,
        "evidence_ids": evidence_ids,
        "source_map": source_map,
        "packet_sha256": canonical_hash(frozen),
        "policy_sha256": _POLICY_SHA256,
        "evidence_block_sha256": _sha(block),
        "char_count": len(block),
    }


def _provenance(provenance):
    _exact(provenance, _PROVENANCE_FIELDS, "provenance")
    origin = provenance["content_origin"]
    if type(origin) is not str or origin not in (
        "editorial_design_draft", "recorded_assistant_output",
    ):
        raise ValueError("content_origin 不在白名单内")
    if type(provenance["natural_view_used"]) is not bool:
        raise ValueError("natural_view_used 必须是布尔值")
    event_ids = provenance["source_event_ids"]
    if type(event_ids) is not list:
        raise ValueError("source_event_ids 必须是字符串列表")
    for event_id in event_ids:
        _text(event_id, "source_event_ids项")
    if origin == "editorial_design_draft":
        if provenance["natural_view_used"] or event_ids:
            raise ValueError("编辑设计稿不能声明使用自然观点或来源事件")
    elif not event_ids:
        raise ValueError("记录的助手输出必须声明非空来源事件列表")
    # 这里只校验声明结构；真实事件、自然观点和语义必须由下游独立核验。
    return deepcopy(provenance)


def _bounds(min_chars, max_chars, tolerance):
    if type(min_chars) is not int or type(max_chars) is not int:
        raise TypeError("字符数边界必须是整数且不能是布尔值")
    if min_chars < 0 or max_chars < min_chars:
        raise ValueError("字符数边界必须满足0 <= min_chars <= max_chars")
    if type(tolerance) not in (int, float):
        raise TypeError("tolerance 必须是有限数值且不能是布尔值")
    if not 0 <= tolerance <= 1 or not math.isfinite(tolerance):
        raise ValueError("tolerance 必须为0至1之间的有限数值")


def _visible(block, additional_text):
    return "阅读材料\n" + block + "\n\n阅读说明\n" + additional_text + "\n\n" + RESPONSE_PROMPT


def _pair_sha256(pair):
    unsigned = deepcopy(pair)
    unsigned["checks"]["integrity"].pop("pair_sha256", None)
    return canonical_hash(unsigned)


def make_dose_pair(
    packet, source_index, i4_text: str, i5_text: str, *, provenance: dict,
    min_chars=600, max_chars=800, tolerance=.1,
) -> dict:
    """仅装配调用方原文；总长和附加段分别按abs差/min长度校验，超限只记偏差。"""
    _bounds(min_chars, max_chars, tolerance)
    provenance = _provenance(provenance)
    _text(i4_text, "i4_text")
    _text(i5_text, "i5_text")
    frozen = validate_packet(packet)
    compact = compact_evidence(frozen, source_index)
    pair = {}
    deviations = []
    for arm, additional_text in (("I4", i4_text), ("I5", i5_text)):
        visible_text = _visible(compact["evidence_block"], additional_text)
        if _ARM_LABEL.search(visible_text):
            raise ValueError("受试可见文本不允许包含处理臂标签；不得通过删改正文规避")
        delivery = deepcopy(compact)
        delivery.update(
            additional_text=additional_text,
            additional_text_sha256=_sha(additional_text),
            additional_chars=len(additional_text),
            response_prompt=RESPONSE_PROMPT,
            visible_text=visible_text,
            char_count=len(visible_text),
            response_opportunities=1,
            contains_treatment_label=False,
            review_status="pending",
            formal_eligible=False,
        )
        pair[arm] = delivery
        if not min_chars <= delivery["char_count"] <= max_chars:
            deviations.append({
                "code": "dose_out_of_range", "arm": arm,
                "char_count": delivery["char_count"], "min_chars": min_chars,
                "max_chars": max_chars,
            })
    left, right = pair["I4"], pair["I5"]
    total_ratio = abs(left["char_count"] - right["char_count"]) / min(
        left["char_count"], right["char_count"],
    )
    additional_ratio = abs(left["additional_chars"] - right["additional_chars"]) / min(
        left["additional_chars"], right["additional_chars"],
    )
    checks = {
        "evidence_equal": all(left[key] == right[key] for key in (
            "evidence_ids", "evidence_block", "packet_sha256", "evidence_block_sha256",
        )),
        "source_equal": left["source_map"] == right["source_map"]
                        and left["policy_sha256"] == right["policy_sha256"],
        "dose_within_range": all(min_chars <= pair[arm]["char_count"] <= max_chars
                                 for arm in ("I4", "I5")),
        "length_matched": total_ratio <= tolerance,
        "additional_length_matched": additional_ratio <= tolerance,
        "total_difference_ratio": total_ratio,
        "additional_difference_ratio": additional_ratio,
        "responses_equal": left["response_opportunities"] == right["response_opportunities"] == 1
                           and left["response_prompt"] == right["response_prompt"] == RESPONSE_PROMPT,
        "length_basis": _LENGTH_BASIS,
        "semantic_review": "pending_independent_review",
        "natural_view_available": provenance["natural_view_used"],
        "formal_eligible": False,
        "deviations": deviations,
    }
    for code, ratio in (("length_mismatch", total_ratio),
                        ("additional_length_mismatch", additional_ratio)):
        if ratio > tolerance:
            deviations.append({"code": code, "difference_ratio": ratio, "tolerance": tolerance})
    checks["assembly_valid"] = all(checks[name] for name in _CHECK_NAMES)
    # delivery没有原始packet，单靠packet_sha256无法重算。因此在检查区保存纯审计
    # envelope；它绝不进入messages。来源映射摘要覆盖日期、完整URL及来源族。
    checks["integrity"] = {
        "packet": deepcopy(frozen),
        "source_map_sha256": canonical_hash(compact["source_map"]),
        "min_chars": min_chars,
        "max_chars": max_chars,
        "tolerance": tolerance,
    }
    pair["checks"] = checks
    pair["provenance"] = provenance
    checks["integrity"]["pair_sha256"] = _pair_sha256(pair)
    return pair


def public_payload(pair, arm) -> dict:
    """验证整对交付后仅导出一条消息，不发送；偏差不阻止离线预览，资格始终为假。

    payload_hash是除自身外整个返回对象的规范JSON哈希；source_map_hash也是
    规范JSON哈希，正文类SHA-256则针对未规范化的UTF-8字节。
    """
    if type(arm) is not str or arm not in ("I4", "I5"):
        raise ValueError("arm 必须是I4或I5")
    _exact(pair, ("I4", "I5", "checks", "provenance"), "pair")
    snapshot = deepcopy(pair)
    _exact(snapshot["checks"], _CHECK_FIELDS, "checks")
    integrity = snapshot["checks"]["integrity"]
    _exact(integrity, _INTEGRITY_FIELDS, "checks.integrity")
    # 普通摘要只能发现未同步重算摘要的改动，不证明来源真实性，不能抵御整对重签。
    # 更强的下游接口应额外接收来自可信冻结清单的expected_pair_sha256（或签名），
    # 并独立核对packet、source_index文件摘要及事件记录，而不是信任请求自带摘要。
    # 本接口不读sourcefile，文件后来变动须由该外部冻结清单校验；这里校验导出快照。
    try:
        if integrity["pair_sha256"] != _pair_sha256(snapshot):
            raise ValueError("交付对摘要校验失败")
        for selected in ("I4", "I5"):
            if type(snapshot[selected]) is not dict:
                raise ValueError("delivery 必须是对象")
        source_map = snapshot["I4"]["source_map"]
        if type(source_map) is not list or not source_map:
            raise ValueError("source_map 必须是非空记录列表")
        index = {}
        for record in source_map:
            _exact(record, _SOURCE_FIELDS, "source_map项")
            sid = _text(record["source_id"], "source_id")
            if sid in index:
                raise ValueError("source_map 不允许重复source_id")
            index[sid] = record
        if integrity["source_map_sha256"] != canonical_hash(source_map):
            raise ValueError("来源映射摘要校验失败")
        expected = make_dose_pair(
            integrity["packet"], index,
            snapshot["I4"]["additional_text"], snapshot["I5"]["additional_text"],
            provenance=snapshot["provenance"], min_chars=integrity["min_chars"],
            max_chars=integrity["max_chars"], tolerance=integrity["tolerance"],
        )
        # 完整重算两臂所有delivery哈希、原文拼装、计数和检查；JSON比较也区分True与1。
        if canonical_hash(snapshot) != canonical_hash(expected):
            raise ValueError("delivery哈希、拼装、计数或检查结果与冻结输入不一致")
    except (KeyError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
        raise ValueError("交付对结构或完整性无效") from exc
    payload = {
        "messages": [{"role": "user", "content": snapshot[arm]["visible_text"]}],
        "source_map_hash": integrity["source_map_sha256"],
        "experimental_ready": False,
    }
    payload["payload_hash"] = canonical_hash(payload)
    return payload
