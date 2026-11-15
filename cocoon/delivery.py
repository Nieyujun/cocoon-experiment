"""独立短投递探测；不修改父报告、不裁剪、不重试、不发送给受试者。

结构校验仅为固定语法规则，不保证语义支持、来源真实性或完整 I4。
"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
from pathlib import Path
import re
import uuid

from .budget import BudgetError
from .contracts import validate_packet
from .dose_build import source_index_from_review
from .dosage import DISPLAY_POLICY, RESPONSE_PROMPT, compact_evidence, make_dose_pair, public_payload, _text
from .evidence import verify_bundle
from .live_audit import audit_live_run
from .mimo import MiMoError, MiMoTransport, _has_key, load_key
from .mimo_probe import LEDGER_RELATIVE, _ledger, _source_check, mimo_status
from .runtime import EventStore, _json_text, _parse_json, _reject_sensitive_keys, canonical_hash, utc_now, verify_events, write_json_new


MODES = ("organized_reflection", "plain_restatement")
STAGES = ("delivery_organized", "delivery_plain")
_POLICY_FIXED = {
    "schema_version": "1.0", "version": "report_to_delivery_v1",
    "display_policy": "source_id_inline_v1", "total_min_chars": 600,
    "total_max_chars": 800, "first_addition_min_chars": 225,
    "first_addition_max_chars": 245, "match_tolerance": 0.1,
    "max_completion_tokens": 1024, "automatic_retries": 0,
    "human_reviews_completed": 0, "formal_eligible": False,
}
_POLICY_FIXED_V2 = {
    **_POLICY_FIXED, "version": "report_to_delivery_v2",
    "first_addition_min_chars": 1, "first_addition_max_chars": 800,
    "sentence_count": 4, "preferred_target_chars": 235,
}
_POLICY_FIXED_V3 = {**_POLICY_FIXED_V2, "version": "report_to_delivery_v3"}
_POLICIES = {"report_to_delivery_v1": _POLICY_FIXED,
             "report_to_delivery_v2": _POLICY_FIXED_V2,
             "report_to_delivery_v3": _POLICY_FIXED_V3}
_POLICIES_WITH_SENTENCE_BUDGET = frozenset(("report_to_delivery_v2", "report_to_delivery_v3"))
_POLICY_TEXT = ("scope", "common_prompt", "protocol_note")
# v1 的保守字面规则；不会把词汇检查包装成事实或语义审计。
_FORBIDDEN = re.compile(
    r"http|www|I[45]|你|您|用户|权威|可信|可靠性认证|公信力|个性化镜像|"
    r"已审核|已正式使用|审核通过|请(?:回答|回应|作答)|"
    r"(?:^|\n)\s*(?:#|[-*]\s)|```|\*\*|"
    r"(?<![A-Z0-9_])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_[ESF]\d+(?![A-Z0-9_])|"
    r"(?<![A-Z0-9_])F_[A-Z0-9_]+|"
    r"(?:^|\n)\s*(?:标题|阅读材料|阅读说明)\s*[:：]", re.IGNORECASE,
)
_CODES = frozenset({
    "policy_hash_mismatch", "invalid_policy", "invalid_packet", "invalid_json",
    "invalid_schema", "invalid_citations", "incomplete_citations", "forbidden_text",
    "invalid_mode", "invalid_questions", "invalid_char_range", "length_out_of_range",
    "empty_char_range", "source_packet_mismatch", "source_audit_failed",
    "source_validation_failed", "source_changed", "missing_delivery_parent",
    "existing_ledger_required", "invalid_model_settings", "credential_in_request",
    "delivery_validation_or_io_error", "audit_error", "invalid_sentence_budget",
    "sentence_length_out_of_range",
})
_BUDGET_CODES = frozenset({
    "probe_authorization_required", "budget_exceeded", "budget_blocked", "budget_busy",
    "budget_io_error", "billing_anomaly", "unresolved_attempt", "ledger_corrupt",
    "limit_mismatch", "invalid_path", "invalid_usage", "duplicate_request",
    "invalid_max_completion_tokens", "invalid_request_id", "unknown_request",
    "already_settled", "invalid_limit", "invalid_reason",
})


class DeliveryError(ValueError):
    """只暴露固定短码与整数边界，绝不回显模型正文或异常原文。"""

    def __init__(self, code, safe_details=None):
        self.code = code if code in _CODES else "delivery_validation_or_io_error"
        self.safe_details = {k: v for k, v in (safe_details or {}).items()
                             if k in ("len", "min", "max") and type(v) is int}
        super().__init__(self.code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def load_delivery_policy(path, expected_sha) -> dict:
    """绑定完整 UTF-8 文件字节；按版本严格接受冻结配置的形状、类型和数值。"""
    try:
        data = Path(path).read_bytes()
        if (type(expected_sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
                or _sha(data) != expected_sha):
            raise DeliveryError("policy_hash_mismatch")
        policy = _parse_json(data)
        if type(policy) is not dict or type(policy.get("version")) is not str:
            raise DeliveryError("invalid_policy")
        fixed = _POLICIES.get(policy["version"])
        if fixed is None or set(policy) != set(fixed) | set(_POLICY_TEXT) | {"mode_prompts"}:
            raise DeliveryError("invalid_policy")
        if any(type(policy[k]) is not type(v) or policy[k] != v for k, v in fixed.items()):
            raise DeliveryError("invalid_policy")
        prompts = policy["mode_prompts"]
        if type(prompts) is not dict or set(prompts) != set(MODES):
            raise DeliveryError("invalid_policy")
        for value in [policy[k] for k in _POLICY_TEXT] + list(prompts.values()):
            if type(value) is not str or not value.strip():
                raise DeliveryError("invalid_policy")
            value.encode("utf-8")
        _reject_sensitive_keys(policy)
        return policy
    except DeliveryError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise DeliveryError("invalid_policy") from None


def _range(char_range):
    if type(char_range) is dict and set(char_range) == {"min", "max"}:
        low, high = char_range["min"], char_range["max"]
    elif type(char_range) in (tuple, list) and len(char_range) == 2:
        low, high = char_range
    else:
        raise DeliveryError("invalid_char_range")
    if type(low) is not int or type(high) is not int or not 0 <= low <= high:
        raise DeliveryError("invalid_char_range")
    return low, high


def plan_sentence_budget(packet, char_range, target) -> dict:
    """按四个固定引用槽分配正文预算，不补字、不裁剪模型输出。"""
    low, high = _range(char_range)
    if type(target) is not int:
        raise DeliveryError("invalid_char_range")
    try:
        frozen = validate_packet(packet, allow_demo=False)
        if len(frozen["evidence"]) != 3:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise DeliveryError("invalid_packet") from None
    ids = [row["evidence_id"] for row in frozen["evidence"]]
    citations = [ids[0], ids[1], ids[2], ids[2]]
    citation_total = sum(len(cid) + 2 for cid in citations)
    text_min = max(1, (low - citation_total + 3) // 4)
    text_max = min(64, (high - citation_total) // 4)
    if text_min > text_max:
        raise DeliveryError("empty_char_range")
    target = min(max(target, 4 * text_min + citation_total), 4 * text_max + citation_total)
    text_target = min(max(round((target - citation_total) / 4), text_min), text_max)
    return {"version": "four_sentence_budget_v2", "slots": [
        {"index": number, "citation_id": cid, "text_min_chars": text_min,
         "text_max_chars": text_max, "target_text_chars": text_target}
        for number, cid in enumerate(citations)],
        "rendered_citation_chars": citation_total, "sentence_count": 4}


def _policy_budget(budget, packet, char_range):
    # 重新推导引用和硬边界，不能由调用者放宽预算或跳过证据覆盖。
    expected = plan_sentence_budget(packet, char_range, _range(char_range)[0])
    if (type(budget) is not dict or set(budget) != set(expected)
            or type(budget["version"]) is not str or budget["version"] != expected["version"]
            or type(budget["sentence_count"]) is not int or budget["sentence_count"] != 4
            or type(budget["rendered_citation_chars"]) is not int
            or budget["rendered_citation_chars"] != expected["rendered_citation_chars"]
            or type(budget["slots"]) is not list or len(budget["slots"]) != 4):
        raise DeliveryError("invalid_sentence_budget")
    for slot, fixed in zip(budget["slots"], expected["slots"]):
        if (type(slot) is not dict or set(slot) != set(fixed)
                or any(type(slot[k]) is not type(v) for k, v in fixed.items())
                or any(slot[k] != v for k, v in fixed.items() if k != "target_text_chars")
                or not slot["text_min_chars"] <= slot["target_text_chars"] <= slot["text_max_chars"]
                or slot["target_text_chars"] != budget["slots"][0]["target_text_chars"]):
            raise DeliveryError("invalid_sentence_budget")
    return deepcopy(budget)


def render_addition(parsed, packet, mode, char_range, *, sentence_budget=None) -> dict:
    """原文逐句加引用、无分隔拼接；不修复合法 JSON 或超限正文。"""
    low, high = _range(char_range)
    if type(mode) is not str or mode not in MODES:
        raise DeliveryError("invalid_mode")
    try:
        frozen = validate_packet(packet, allow_demo=False)
        if len(frozen["evidence"]) != 3:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise DeliveryError("invalid_packet") from None
    if sentence_budget is not None:
        sentence_budget = _policy_budget(sentence_budget, frozen, char_range)
    if type(parsed) in (str, bytes):
        try:
            parsed = _parse_json(parsed)
        except (ValueError, RecursionError):
            raise DeliveryError("invalid_json") from None
    if (type(parsed) is not dict or set(parsed) != {"schema_version", "packet_id", "mode", "sentences"}
            or parsed["schema_version"] != "1.0" or parsed["packet_id"] != frozen["packet_id"]
            or parsed["mode"] != mode):
        raise DeliveryError("invalid_schema")
    sentences = parsed["sentences"]
    if (type(sentences) is not list or not 3 <= len(sentences) <= 5
            or (sentence_budget is not None and len(sentences) != 4)):
        raise DeliveryError("invalid_schema")
    ids = {row["evidence_id"] for row in frozen["evidence"]}
    markers = ids | {row["source_family_id"] for row in frozen["evidence"]}
    markers |= {frozen["packet_id"], frozen["topic_id"]}
    covered, texts, rendered = set(), set(), []
    questions = 0
    sentence_length_error = None
    for number, sentence in enumerate(sentences):
        if type(sentence) is not dict or set(sentence) != {"text", "citations", "kind"}:
            raise DeliveryError("invalid_schema")
        kind, text, citations = sentence["kind"], sentence["text"], sentence["citations"]
        if type(kind) is not str or kind not in ("paraphrase", "limitation", "question"):
            raise DeliveryError("invalid_schema")
        if (type(citations) is not list or not citations
                or any(type(cid) is not str or cid not in ids for cid in citations)
                or len(citations) != len(set(citations))):
            raise DeliveryError("invalid_citations")
        try:
            _text(text, "text")
        except (ValueError, TypeError):
            raise DeliveryError("forbidden_text") from None
        if (_FORBIDDEN.search(text) or any(marker.casefold() in text.casefold() for marker in markers)
                or not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text) or text in texts):
            raise DeliveryError("forbidden_text")
        if sentence_budget is not None:
            slot = sentence_budget["slots"][number]
            if citations != [slot["citation_id"]]:
                raise DeliveryError("invalid_citations")
            if not slot["text_min_chars"] <= len(text) <= slot["text_max_chars"] and sentence_length_error is None:
                sentence_length_error = DeliveryError("sentence_length_out_of_range", {
                    "len": len(text), "min": slot["text_min_chars"], "max": slot["text_max_chars"]})
            if mode == MODES[0] and (
                    (number < 3 and kind == "question")
                    or (number == 3 and (kind != "question" or not text.endswith(("?", "？"))))):
                raise DeliveryError("invalid_questions")
        questions += kind == "question"
        if mode == MODES[1] and (kind == "question" or "?" in text or "？" in text):
            raise DeliveryError("invalid_questions")
        texts.add(text)
        covered.update(citations)
        rendered.append(text + "（" + "、".join(citations) + "）")
    if covered != ids:
        raise DeliveryError("incomplete_citations")
    if mode == MODES[0] and questions not in (1, 2):
        raise DeliveryError("invalid_questions")
    additional_text = "".join(rendered)
    if not low <= len(additional_text) <= high:
        raise DeliveryError("length_out_of_range", {"len": len(additional_text), "min": low, "max": high})
    if sentence_length_error is not None:
        raise sentence_length_error
    return {"structured_output": deepcopy(parsed), "additional_text": additional_text,
            "char_count": len(additional_text), "char_range": {"min": low, "max": high},
            "covered_evidence_ids": sorted(covered), "question_count": questions,
            "syntax_only": True, "semantic_guarantee": False, "text_unchanged": True}


def _guard(value):
    if _has_key(value, load_key()):
        raise MiMoError("credential_in_request")
    _reject_sensitive_keys(value)


class _DeliveryStore(EventStore):
    # 传输器也使用这个实例；凭据仅临时读取，不成为 store 属性或 blob。
    def capture(self, obj):
        _guard(obj)
        return super().capture(obj)

    def append(self, event_type, **fields):
        _guard(fields)
        return super().append(event_type, **fields)


def _run_hashes(root):
    return {p.relative_to(root).as_posix(): _sha(p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file()}


def _bind_packet(build, packet_path):
    manifest_bytes = (build / "manifest.json").read_bytes()
    manifest = _parse_json(manifest_bytes)
    verified = verify_bundle(build)
    packet_bytes = packet_path.read_bytes()
    packet = validate_packet(_parse_json(packet_bytes), allow_demo=False)
    if len(packet["evidence"]) != 3 or len(manifest["topics"]) != 3:
        raise DeliveryError("source_packet_mismatch")
    files = {row["path"]: row for row in manifest["files"]}
    if len(files) != len(manifest["files"]):
        raise DeliveryError("source_packet_mismatch")
    names, matches = set(), []
    for topic in manifest["topics"]:
        for round_no in (1, 2):
            name = topic[f"round_{round_no}_packet"]
            path = (build / name).resolve(strict=True)
            if name in names or not path.is_relative_to(build) or name not in files:
                raise DeliveryError("source_packet_mismatch")
            names.add(name)
            data = path.read_bytes()
            item = validate_packet(_parse_json(data), allow_demo=False)
            if (len(item["evidence"]) != 3 or item["topic_id"] != topic["topic_id"]
                    or _sha(data) != files[name]["sha256"]):
                raise DeliveryError("source_packet_mismatch")
            if data == packet_bytes and canonical_hash(item) == canonical_hash(packet):
                matches.append(name)
    if len(names) != 6 or len(matches) != 1 or (build / "manifest.json").read_bytes() != manifest_bytes:
        raise DeliveryError("source_packet_mismatch")
    return packet, {"source_path": str(packet_path), "file_sha256": _sha(packet_bytes),
                    "canonical_sha256": canonical_hash(packet), "frozen_path": matches[0],
                    "manifest_source_path": str(build / "manifest.json"),
                    "manifest_sha256": _sha(manifest_bytes), "verification": verified}


def _intersection(low, high, allowed):
    low, high = max(low, allowed["min"]), min(high, allowed["max"])
    if low > high or low < 1:
        raise DeliveryError("empty_char_range")
    return {"min": low, "max": high}


def _failure(exc):
    if isinstance(exc, DeliveryError):
        return {"code": exc.code, "safe_details": exc.safe_details}
    if isinstance(exc, MiMoError):
        return {"code": exc.code, "safe_details": exc.details}
    if isinstance(exc, BudgetError):
        return {"code": exc.code if exc.code in _BUDGET_CODES else "budget_error", "safe_details": {}}
    return {"code": "delivery_validation_or_io_error", "safe_details": {}}


def _billing(ledger):
    try:
        return ledger.status()
    except BudgetError as exc:
        return {"error": _failure(exc), "charged_cny": None, "total_cost_cny": None,
                "needs_reconciliation": True}


def _cost(before, after):
    with localcontext() as context:
        context.prec = 50
        known = (str(Decimal(after["charged_cny"]) - Decimal(before["charged_cny"]))
                 if before.get("charged_cny") is not None and after.get("charged_cny") is not None else None)
        actual = (str(Decimal(after["total_cost_cny"]) - Decimal(before["total_cost_cny"]))
                  if before.get("total_cost_cny") is not None and after.get("total_cost_cny") is not None else None)
    return {"known_cost_cny": known, "total_cost_cny": actual,
            "ledger_uncertain": after.get("needs_reconciliation", True),
            "basis": "累计账本 Decimal 差值；供应商 usage 记账，非最终账单"}


def run_delivery(config, root, *, run_directory: Path, packet_path: Path,
                 evidence_build: Path, policy_path: Path, expected_policy_sha: str,
                 execute=False) -> dict:
    """显式执行才创建新运行；调用方负责授权，本模块没有隐式执行或重试。"""
    result = {"schema_version": "1.0", "status": "blocked", "executed": False,
              "run_id": None, "run_directory": None, "actual_model_calls": 0,
              "actual_http_attempts": 0, "requests_started": 0, "completed_model_calls": 0,
              "validated_stages": 0, "failed_stage": None, "error": None, "blockers": [],
              "syntax_only": True, "semantic_guarantee": False, "full_I4_eligible": False,
              "mirror_executed": False, "mirror": False, "natural_view_used": False,
              "received_actual_subject": False, "formal_eligible": False,
              "human_reviews_completed": 0, "external_retrieval_calls": 0,
              "automatic_retries": 0, "is_demo": False, "demo_fallback": False,
              "internalreport_unchanged": None, "inputs_unchanged": None,
              "policy_version": None, "additions": {}, "lengths": {},
              "source_event_ids": [], "pair": None, "payloads": {}, "cost": None,
              "model_call_count_basis": "actual_http_attempts_not_requests_started_or_billing_confirmation"}
    try:
        # 哈希失败先于账本打开；已有账本是硬前提，不得借新路径恢复额度。
        policy = load_delivery_policy(policy_path, expected_policy_sha)
        result["policy_version"] = policy["version"]
        _source_check(config)
        root = Path(root).resolve(strict=True)
        ledger_path = root / LEDGER_RELATIVE
        if not ledger_path.is_file() or ledger_path.stat().st_size == 0:
            raise DeliveryError("existing_ledger_required")
        state = mimo_status(config, root)
        result.update(expected_policy_sha256=expected_policy_sha, readiness=state,
                      billing_before=state["billing"], billing_after=state["billing"],
                      cost=_cost(state["billing"], state["billing"]))
        if execute is not True or state["blockers"]:
            result["blockers"] = list(state["blockers"])
            if execute is not True:
                result["blockers"].append("execute_required")
            return result
        _guard(config)
        _guard(policy)
        parent = root / "experiment/delivery_probes"
        if not parent.is_dir() or parent.is_symlink() or not parent.resolve().is_relative_to(root):
            raise DeliveryError("missing_delivery_parent")
        source = Path(run_directory).resolve(strict=True)
        build = Path(evidence_build).resolve(strict=True)
        packet_path = Path(packet_path).resolve(strict=True)
        before_hashes = _run_hashes(source)
        packet, packet_info = _bind_packet(build, packet_path)
        sources_path = build / "review/sources.json"
        source_index_input = {"source_path": str(sources_path), "file_sha256": _sha(sources_path.read_bytes())}
        index = source_index_from_review(build)
        assistant = config["assistant"]
        try:
            audit = audit_live_run(source, packet_path, assistant["prompt_bundle_path"],
                                   assistant["prompt_bundle_sha256"], index)
        except ValueError:
            raise DeliveryError("source_audit_failed") from None
        if (audit["audit_status"] != "passed" or audit["run_status"] != "succeeded"
                or audit["four_stage_structure_passed"] is not True or audit["mirror_executed"] is not False
                or audit["natural_view_used"] is not False or audit["actual_http_attempts"] != 4
                or audit["expected_packet_hash"] != canonical_hash(packet)
                or audit["summary_sha256"] != before_hashes.get("summary.json")
                or any(not audit["stage_results"][stage]["structure_passed"]
                       for stage in ("search", "label", "blindspot", "integrate"))):
            raise DeliveryError("source_audit_failed")
        report = deepcopy(audit["stage_results"]["integrate"]["structured_output"])
        compact = compact_evidence(packet, index)
        overhead = len("阅读材料\n\n\n阅读说明\n\n\n" + RESPONSE_PROMPT)
        if overhead != 39:
            raise DeliveryError("invalid_policy")
        fixed = compact["char_count"] + overhead
        allowed = {"min": policy["total_min_chars"] - fixed, "max": policy["total_max_chars"] - fixed}
        first_range = _intersection(policy["first_addition_min_chars"], policy["first_addition_max_chars"], allowed)
        is_v2 = policy["version"] in _POLICIES_WITH_SENTENCE_BUDGET
        model = config["models"]["assistant"]
        if type(model["max_tokens"]) is not int or not 1024 <= model["max_tokens"] <= 4096:
            raise DeliveryError("invalid_model_settings")
        settings = {k: model[k] for k in ("provider", "model", "temperature", "max_tokens")}
        settings["max_tokens"] = 1024
        metadata = {"policy_sha256": expected_policy_sha, "policy_version": policy["version"], "model_settings": settings,
                    "parent_run_id": audit["run_id"], "parent_summary_sha256": audit["summary_sha256"],
                    "parent_report_sha256": canonical_hash(report), "packet_sha256": canonical_hash(packet),
                    "source_index_sha256": canonical_hash(index), "execution_mode": "delivery_probe",
                    "thinking": "disabled", "automatic_retries": 0}
        policy_input = {"source_path": str(Path(policy_path).resolve()), "file_sha256": expected_policy_sha}
        _guard({"packet": packet, "source_index": index, "report": report, "metadata": metadata,
                "parent_file_hashes": before_hashes, "policy_input": policy_input,
                "parent_raw_report": audit["stage_results"]["integrate"]["raw_text"],
                "packet_info": packet_info, "source_index_input": source_index_input,
                "source_path": str(source), "root_path": str(root)})
        input_hashes = {packet_path: packet_info["file_sha256"],
                        build / "manifest.json": packet_info["manifest_sha256"],
                        Path(policy_input["source_path"]): policy_input["file_sha256"],
                        sources_path: source_index_input["file_sha256"]}

        def check_inputs():
            try:
                parent_unchanged = _run_hashes(source) == before_hashes
            except OSError:
                parent_unchanged = False
            result["internalreport_unchanged"] = (result["internalreport_unchanged"] is not False
                and parent_unchanged and canonical_hash(report) == metadata["parent_report_sha256"])
            files_unchanged = True
            for path, digest in input_hashes.items():
                try:
                    unchanged = _sha(path.read_bytes()) == digest
                except OSError:
                    unchanged = False
                files_unchanged = unchanged and files_unchanged
            result["inputs_unchanged"] = (result["inputs_unchanged"] is not False
                and result["internalreport_unchanged"] and files_unchanged)
            if not result["inputs_unchanged"]:
                raise DeliveryError("source_changed")

        check_inputs()
        ledger = _ledger(config, root)
        billing_before = ledger.status()
    except (DeliveryError, MiMoError, BudgetError, ValueError, TypeError, OSError, KeyError,
            IndexError, StopIteration, RecursionError, OverflowError) as exc:
        result["error"] = _failure(exc)
        result["blockers"] = [result["error"]["code"]]
        return result

    run_id = "delivery_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex
    out = parent / run_id
    out.mkdir()
    store = _DeliveryStore(out, metadata, canonical_hash(packet), False, run_id)
    transport = MiMoTransport(model, store, ledger)
    result.update(status="delivery_failed", executed=True, run_id=run_id, run_directory=str(out),
                  expected_policy_sha256=expected_policy_sha, policy_input=policy_input, model_settings=settings,
                  billing_before=billing_before, billing_source_path=str(ledger_path), parent_run_id=audit["run_id"],
                  parent_source_path=str(source), parent_summary_sha256=audit["summary_sha256"],
                  parent_report_sha256=canonical_hash(report), packet_input=packet_info,
                  source_index_input=source_index_input,
                  original_internal_report={"structured_chars": len(_json_text(report)),
                      "rendered_chars": audit["report_chars"], "raw_chars": len(audit["stage_results"]["integrate"]["raw_text"]),
                      "raw_text_sha256": _sha(audit["stage_results"]["integrate"]["raw_text"].encode("utf-8")),
                      "length_constraint": None, "source_event_ids": audit["stage_results"]["integrate"]["source_event_ids"]},
                  fixed_chars=fixed, evidence_chars=compact["char_count"], public_constant_chars=overhead,
                  allowed_additional_char_range=allowed, expected_assembly={},
                  expected_counts={"actual_http_attempts": 2, "completed_model_calls": 2, "validated_stages": 2})
    stage = "preflight"
    try:
        result["inputs"] = {"policy": store.capture(policy), "packet": store.capture(packet),
                            "source_index": store.capture(index), "internal_report": store.capture(report),
                            "parent_file_hashes": store.capture(before_hashes), "configuration": store.capture(metadata)}
        store.append("delivery_started", payload={"inputs": result["inputs"], "configuration": metadata,
                     "billing_before": billing_before, "received_actual_subject": False})
        for mode, stage in zip(MODES, STAGES):
            if mode == MODES[0]:
                char_range = first_range
                target = (min(max(policy["preferred_target_chars"], char_range["min"]), char_range["max"])
                          if is_v2 else (char_range["min"] + char_range["max"]) // 2)
            else:
                size = result["additions"][MODES[0]]["rendered"]["char_count"]
                char_range = _intersection((10 * size + 10) // 11, 11 * size // 10, allowed)
                target = size
            sentence_budget = plan_sentence_budget(packet, char_range, target) if is_v2 else None
            budget_sha = canonical_hash(sentence_budget) if is_v2 else None
            user = {"frozen_evidence_packet": packet, "mode": mode,
                    "additional_char_range": char_range, "target_chars": target}
            if is_v2:
                user["sentence_budget"] = sentence_budget
            if mode == MODES[0]:
                user["internal_report"] = report
            request = {"request_id": f"{run_id}:{stage}:1", "stage": stage, "is_demo": False,
                       "model_settings": settings, "messages": [
                           {"role": "system", "content": policy["common_prompt"] + "\n" + policy["mode_prompts"][mode]},
                           {"role": "user", "content": _json_text(user)}]}
            _guard(request)
            check_inputs()
            row = {"stage": stage, "request_id": request["request_id"], "status": "started",
                   "policy_version": policy["version"], "sentence_budget": deepcopy(sentence_budget),
                   "sentence_budget_sha256": budget_sha,
                   "additional_char_range": char_range, "target_chars": target,
                   "request": store.capture(request), "response": None, "rendered": None,
                   "transport_records": []}
            result["additions"][mode] = row
            store.append("delivery_request_started", phase=stage, input_ref=row["request"],
                         payload={"request_id": request["request_id"], "http_attempted": False})
            check_inputs()
            response = transport.complete(request)
            # 先保存全部七字段归一化响应，再解析模型正文；拒绝时不丢结果。
            row["response"] = store.capture(response)
            received = store.append("delivery_response_received", phase=stage, input_ref=row["request"],
                                    output_ref=row["response"], payload={"validated": False})
            row["source_event_id"] = received["event_id"]
            check_inputs()
            if is_v2 and canonical_hash(sentence_budget) != budget_sha:
                raise DeliveryError("invalid_sentence_budget")
            try:
                parsed = _parse_json(response["raw_text"])
            except (ValueError, RecursionError):
                raise DeliveryError("invalid_json") from None
            rendered = render_addition(parsed, packet, mode, char_range, sentence_budget=sentence_budget)
            row.update(status="validated", rendered=rendered, structured=store.capture(parsed))
            result["source_event_ids"].append(received["event_id"])
            store.append("delivery_stage_validated", phase=stage, output_ref=row["structured"],
                         payload={"response": row["response"], "rendered": rendered, "syntax_only": True})
        stage = "assembly"
        texts = [result["additions"][mode]["rendered"]["additional_text"] for mode in MODES]
        pair = make_dose_pair(packet, index, *texts, provenance={
            "content_origin": "recorded_assistant_output", "natural_view_used": False,
            "source_event_ids": result["source_event_ids"]})
        result["pair"] = store.capture(pair)
        payloads = {}
        for arm in ("I4", "I5"):
            payloads[arm] = public_payload(pair, arm)
            result["payloads"][arm] = store.capture(payloads[arm])
            result["lengths"][arm] = {"visible_chars": len(payloads[arm]["messages"][0]["content"]),
                                      "additional_chars": len(pair[arm]["additional_text"])}
        checks = {name: pair["checks"][name] is True for name in (
            "evidence_equal", "source_equal", "dose_within_range", "length_matched",
            "additional_length_matched", "responses_equal", "assembly_valid")}
        checks["exact_assembly"] = all(
            payloads[arm]["messages"][0]["content"] == "阅读材料\n" + compact["evidence_block"]
            + "\n\n阅读说明\n" + text + "\n\n" + RESPONSE_PROMPT
            and pair[arm]["additional_text"] == text and pair[arm]["char_count"] == fixed + len(text)
            for arm, text in zip(("I4", "I5"), texts))
        result.update(expected_assembly=checks, pair_checks=pair["checks"],
                      status="generated_structurally_valid" if all(checks.values()) else "generated_with_deviations")
    except (DeliveryError, MiMoError, BudgetError, ValueError, TypeError, OSError, KeyError,
            IndexError, RecursionError, OverflowError) as exc:
        result.update(status="delivery_failed", failed_stage=stage, error=_failure(exc))
        if stage in STAGES and MODES[STAGES.index(stage)] in result["additions"]:
            result["additions"][MODES[STAGES.index(stage)]]["status"] = "failed"

    # 不以 started 或预留数量代替真实 HTTP 计数；传输失败的 HTTP blob 也保留关联。
    events = [_parse_json(line) for line in (out / "events/events.jsonl").read_bytes().splitlines()]
    for row in result["additions"].values():
        row["transport_records"] = [{"event_id": e["event_id"], "event_type": e["event_type"],
            "input": {"ref": e["input_ref"], "sha256": e["input_sha256"]} if "input_ref" in e else None,
            "output": {"ref": e["output_ref"], "sha256": e["output_sha256"]} if "output_ref" in e else None}
            for e in events if e.get("task_id") == row["request_id"] and e["event_type"].startswith("mimo_")]
        row["raw_response"] = row["response"] or next((e["output"] for e in row["transport_records"]
            if e["event_type"] in ("mimo_response_received", "mimo_response_omitted")), None)
    result.update(actual_model_calls=transport.actual_model_calls, actual_http_attempts=transport.actual_model_calls,
                  requests_started=sum(e["event_type"] == "delivery_request_started" for e in events),
                  completed_model_calls=sum(e["event_type"] == "mimo_request_completed" for e in events),
                  validated_stages=sum(e["event_type"] == "delivery_stage_validated" for e in events),
                  billing_after=_billing(ledger), completed_at_utc=utc_now())
    result["last_counts"] = {k: result[k] for k in ("actual_http_attempts", "requests_started",
                                                    "completed_model_calls", "validated_stages")}
    result["cost"] = _cost(billing_before, result["billing_after"])
    result["billing_delta"] = result["cost"]
    result["expected_counts_passed"] = all(result[k] == v for k, v in result["expected_counts"].items())
    if result["status"] == "generated_structurally_valid" and (not result["expected_counts_passed"]
            or result["cost"]["total_cost_cny"] is None):
        result["status"] = "generated_with_deviations"
    if result["failed_stage"] in STAGES:
        failed = result["additions"].get(MODES[STAGES.index(result["failed_stage"])], {})
        result["failed_raw_response"] = failed.get("raw_response")
    # 终态落盘前再次核对，包括解析或传输失败的路径；已计费原始响应不回滚。
    try:
        check_inputs()
    except DeliveryError as exc:
        if result["error"] is not None:
            result["prior_error"] = result["error"]
        result.update(status="delivery_failed", failed_stage="source_integrity", error=_failure(exc))
        if not result.get("failed_raw_response"):
            result["failed_raw_response"] = next((row["raw_response"]
                for row in reversed(list(result["additions"].values())) if row.get("raw_response")), None)
    store.append("delivery_failed" if result["status"] == "delivery_failed" else "delivery_completed",
                 status=result["status"], payload=result)
    result["verification"] = verify_events(out)
    _guard(result)
    write_json_new(out / "summary.json", result)
    return result


def main(argv=None):
    import argparse
    import json
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="独立短投递探测；默认只检查，不调用模型。")
    parser.add_argument("--config", type=Path, default=root / "configs/core.json")
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--evidence-build", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--policy-sha", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_delivery(_parse_json(args.config.read_bytes()), root,
            run_directory=args.source_run, packet_path=args.packet, evidence_build=args.evidence_build,
            policy_path=args.policy, expected_policy_sha=args.policy_sha, execute=args.execute)
    except (ValueError, MiMoError, OSError, TypeError, KeyError, RecursionError):
        result = {"status": "delivery_failed", "failed_stage": "initialization"}
    short = {"status": result["status"], "runpath": result.get("run_directory"),
             "counts": result.get("last_counts", {"actual_http_attempts": result.get("actual_http_attempts", 0)}),
             "lengths": result.get("lengths", {}), "cost": result.get("cost"),
             "failedstage": result.get("failed_stage")}
    print(json.dumps(short, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] == "generated_structurally_valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
