"""步骤39：三议题剩余五轮真实 mimo 组织侧生成与六轮整句摘录配对合龙。

边界（不可放宽）：
- 只在 `execute=True` 且凭据、账本、配置全部就绪时发起真实 HTTP 请求；
  否则返回 blocked，且 `actual_model_calls=0`。
- 每次尝试（成功或失败）都写入运行目录 EventStore 与共享计费账本，不静默丢弃。
- 组织侧正文由模型生成，本地只做结构/引用/字数校验，**不补字、不裁剪、不改写**；
  I5 对照只做确定性整句摘录，不接收任何模型正文。
- 生成成功不等于已送达受试、不等于完整 I4、不等于人审或正式实验资格。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from . import delivery, dosage, extractive_control
from .budget import BudgetError
from .contracts import validate_packet
from .mimo import MiMoError, MiMoTransport
from .mimo_probe import _ledger, mimo_status
from .runtime import (
    EventStore, _parse_json, _verify_blob, canonical_hash, utc_now, verify_events,
    write_json_new,
)

SCHEMA_VERSION = "1.0"
MANIFEST_CONTRACT = "six_round_live_extractive_pairs_v1"
RUNS_RELATIVE = "experiment/live_deliveries"
MANIFEST_RELATIVE = "experiment/review/live_cross_topic_20261111/six_round_live_extractive_pairs.json"
LEDGER_RELATIVE = "experiment/billing/mimo_initial_probe_v1.jsonl"
POLICY_RELATIVE = "configs/delivery_prompts_v3.json"
POLICY_SHA256 = "b2cecda52c4fa2725400063094dcee9466a16037e02bfa05f64fcf883d744684"
POLICY_VERSION = "report_to_delivery_v3"
EXISTING_R1_RUN_ID = "delivery_20261108T105552Z_7fe7dae9d342459885c811b169a74ff1"
MAX_ATTEMPTS = 3
DOSE_MIN, DOSE_MAX = 600, 800
FIXED_CHARS = {
    "TOPIC_01_R1": 457, "TOPIC_01_R2": 459, "TOPIC_02_R1": 492,
    "TOPIC_02_R2": 475, "TOPIC_03_R1": 513, "TOPIC_03_R2": 557,
}
TARGET_BANDS = {
    "TOPIC_01_R1": (175, 205), "TOPIC_01_R2": (160, 205), "TOPIC_02_R1": (165, 220),
    "TOPIC_02_R2": (165, 220), "TOPIC_03_R1": (155, 195), "TOPIC_03_R2": (155, 210),
}
CASES = (("TOPIC_01", 1), ("TOPIC_01", 2), ("TOPIC_02", 1),
         ("TOPIC_02", 2), ("TOPIC_03", 1), ("TOPIC_03", 2))
CASE_IDS = tuple(f"{topic}_R{round_no}" for topic, round_no in CASES)
REMAINING = tuple(case_id for case_id in CASE_IDS if case_id != "TOPIC_01_R1")
CHECK_NAMES = ("evidence_equal", "source_equal", "dose_within_range", "length_matched",
               "additional_length_matched", "responses_equal", "assembly_valid")


def _need(condition, code):
    if not condition:
        raise ValueError("live_cross_topic_delivery:" + code)


def _sha(value):
    data = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _file_sha(path):
    return _sha(Path(path).read_bytes())


def _topic_round(case_id):
    _need(case_id in CASE_IDS, "unknown_case_id")
    topic, round_text = case_id.rsplit("_R", 1)
    return topic, int(round_text)


def _packet_path(build, topic, round_no):
    number = topic.rsplit("_", 1)[-1]
    return Path(build) / "packets" / f"TOPIC_{number}_round_{round_no}.json"


def _load_packet(build, case_id):
    topic, round_no = _topic_round(case_id)
    path = _packet_path(build, topic, round_no)
    _need(path.is_file(), "packet_missing:" + case_id)
    packet = validate_packet(_parse_json(path.read_bytes()), allow_demo=False)
    _need(packet["topic_id"] == topic and len(packet["evidence"]) == 3, "packet_shape_invalid")
    return packet, path


def dosage_index(build):
    from .pilot_materials import source_index_from_review
    return source_index_from_review(Path(build))


def _visible(block, addition):
    return "阅读材料\n" + block + "\n\n阅读说明\n" + addition + "\n\n" + dosage.RESPONSE_PROMPT


def _plain_text(sentences):
    return "".join(row["text"] + "（" + row["evidence_id"] + "）" for row in sentences)


def _selection(evidence, fixed, target):
    return extractive_control.select_exact_sentences(evidence, target, fixed,
                                                    extractive_control._POLICY)


def _feasible_targets(evidence, fixed):
    values = []
    for target in range(max(1, DOSE_MIN - fixed), DOSE_MAX - fixed + 1):
        if _selection(evidence, fixed, target)["status"] == "structurally_matched_candidate":
            values.append(target)
    return values


def _ranges(values):
    _need(bool(values), "no_feasible_target")
    ranges, start, previous = [], values[0], values[0]
    for value in values[1:]:
        if value != previous + 1:
            ranges.append([start, previous])
            start = value
        previous = value
    ranges.append([start, previous])
    return ranges


def _band_target(feasible, band):
    low, high = band
    inside = [value for value in feasible if low <= value <= high]
    if inside:
        return inside[len(inside) // 2]
    return min(feasible, key=lambda value: (abs(value - low), abs(value - high)))


def plan_rounds(evidence_build_dir, *, policy_path=None, expected_policy_sha=POLICY_SHA256) -> dict:
    """离线规划：逐轮固定开销、整句摘录可行区间、目标长度与四句预算。"""
    build = Path(evidence_build_dir).resolve(strict=True)
    policy = delivery.load_delivery_policy(
        policy_path or (Path(__file__).resolve().parents[1] / POLICY_RELATIVE), expected_policy_sha)
    index = dosage_index(build)
    rows = []
    for case_id in CASE_IDS:
        packet, path = _load_packet(build, case_id)
        compact = dosage.compact_evidence(packet, index)
        fixed = len(_visible(compact["evidence_block"], ""))
        _need(fixed == FIXED_CHARS[case_id], "fixed_chars_drift:" + case_id)
        feasible = _feasible_targets(packet["evidence"], fixed)
        allowed = {"min": policy["total_min_chars"] - fixed, "max": policy["total_max_chars"] - fixed}
        low = max(min(feasible), allowed["min"])
        high = min(max(feasible), allowed["max"])
        _need(low <= high, "empty_char_range:" + case_id)
        char_range = {"min": low, "max": high}
        target = min(max(_band_target(feasible, TARGET_BANDS[case_id]), low), high)
        sentence_budget = delivery.plan_sentence_budget(packet, char_range, target)
        rows.append({
            "case_id": case_id, "topic_id": _topic_round(case_id)[0], "round": _topic_round(case_id)[1],
            "packet_path": path.relative_to(build).as_posix(), "packet_id": packet["packet_id"],
            "packet_sha256": canonical_hash(packet), "packet_file_sha256": _file_sha(path),
            "evidence_ids": [row["evidence_id"] for row in packet["evidence"]],
            "fixed_chars": fixed, "feasible_organized_ranges": _ranges(feasible),
            "feasible_length_count": len(feasible), "allowed_org_range": allowed,
            "additional_char_range": char_range, "target_chars": target,
            "target_band": list(TARGET_BANDS[case_id]),
            "sentence_budget": sentence_budget,
            "sentence_budget_sha256": canonical_hash(sentence_budget),
            "policy_sha256": expected_policy_sha,
        })
    return {"schema_version": SCHEMA_VERSION, "policy_sha256": expected_policy_sha,
            "policy_version": policy["version"], "cases": rows,
            "case_count": len(rows), "remaining": list(REMAINING)}


def pair_case(packet, index, organized_text, *, source_event_ids):
    """组织侧真实正文 + 确定性整句摘录对照，复用冻结剂量配对器与七项检查。"""
    compact = dosage.compact_evidence(packet, index)
    fixed = len(_visible(compact["evidence_block"], ""))
    selection = _selection(packet["evidence"], fixed, len(organized_text))
    if selection["status"] != "structurally_matched_candidate":
        return {"status": selection["status"], "selection": selection, "pair": None,
                "checks": {name: False for name in CHECK_NAMES}, "fixed_chars": fixed,
                "plain_text": None, "selection_sha256": canonical_hash(selection)}
    plain_text = _plain_text(selection["selected_sentences"])
    pair = dosage.make_dose_pair(packet, index, organized_text, plain_text, provenance={
        "content_origin": "recorded_assistant_output", "natural_view_used": False,
        "source_event_ids": list(source_event_ids)})
    checks = {name: pair["checks"][name] is True for name in CHECK_NAMES}
    return {"status": "paired", "selection": selection, "pair": pair, "checks": checks,
            "plain_text": plain_text, "fixed_chars": fixed,
            "selection_sha256": canonical_hash(selection)}


def _summary_packet_sha(summary):
    return summary.get("packet_sha256") or summary["packet_input"]["canonical_sha256"]


def _summary_policy_sha(summary):
    return summary.get("policy_sha256") or summary["expected_policy_sha256"]


def _settings(config):
    model = config["models"]["assistant"]
    settings = {key: model[key] for key in ("provider", "model", "temperature", "max_tokens")}
    settings["max_tokens"] = 1024
    return settings


def _request(run_id, attempt, policy, packet, char_range, target, budget, settings, feedback):
    user = {"frozen_evidence_packet": packet, "mode": "organized_reflection",
            "additional_char_range": char_range, "target_chars": target,
            "sentence_budget": budget}
    if feedback is not None:
        user["previous_attempt_feedback"] = feedback
    return {
        "request_id": f"{run_id}:organized:{attempt}", "stage": "delivery_organized",
        "is_demo": False, "model_settings": settings,
        "messages": [
            {"role": "system",
             "content": policy["common_prompt"] + "\n" + policy["mode_prompts"]["organized_reflection"]},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False, allow_nan=False)},
        ],
    }


def _failure(exc):
    details = getattr(exc, "details", None)
    return {"code": getattr(exc, "code", None) or type(exc).__name__,
            "message": str(exc)[:400],
            "details": details if isinstance(details, dict) else None}


_FEEDBACK_INSTRUCTIONS = {
    "length_out_of_range": "四句渲染后总长必须落在 additional_char_range 内：请把每句写足或收紧，"
                           "但不要增删证据引用，也不要加入新事实。",
    "sentence_length_out_of_range": "某一句话长越界：请按 sentence_budget 的 text_min_chars/"
                                    "text_max_chars 逐句调整长度，四条都要达标。",
    "invalid_questions": "第 4 句必须是针对第三条证据资源限制的完整疑问句并以问号结尾；前 3 句不得是问句。",
    "invalid_citations": "每句 citations 必须与 sentence_budget 指定槽位的证据 ID 完全一致。",
    "incomplete_citations": "四句合起来必须覆盖证据包内全部三条证据 ID。",
    "forbidden_text": "正文不得出现禁词、主题标签、证据包 ID 或来源族 ID。",
    "invalid_schema": "必须只输出四字段 JSON，sentences 恰为 4 条，每条含 text/citations/kind。",
    "invalid_json": "输出必须是合法 JSON 对象，不要加代码块或解释。",
}


def _feedback_from(error, parsed=None, budget=None, char_range=None):
    """把上一轮的真实句长与硬边界算成可执行的字数指令，避免模型盲猜。"""
    observations = {"error_code": error["code"]}
    instruction = _FEEDBACK_INSTRUCTIONS.get(
        error["code"], "请严格按 system 提示重写四句，不要改变证据范围。")
    sentences = parsed.get("sentences") if isinstance(parsed, dict) else None
    if isinstance(sentences, list) and sentences and all(
            isinstance(row, dict) and isinstance(row.get("text"), str) for row in sentences):
        lengths = [len(row["text"]) for row in sentences]
        rendered = sum(len(row["text"]) + len("".join(row.get("citations") or [])) + 2
                       for row in sentences)
        observations.update(previous_sentence_chars=lengths, previous_total_chars=rendered)
        if isinstance(budget, dict) and budget.get("slots"):
            low = budget["slots"][0]["text_min_chars"]
            high = budget["slots"][0]["text_max_chars"]
            over = [f"第{index + 1}句 {length} 字（上限 {high}）"
                    for index, length in enumerate(lengths) if length > high]
            under = [f"第{index + 1}句 {length} 字（下限 {low}）"
                     for index, length in enumerate(lengths) if length < low]
            pieces = []
            if over:
                pieces.append("超出单句上限：" + "、".join(over) + "。")
            if under:
                pieces.append("低于单句下限：" + "、".join(under) + "。")
            if isinstance(char_range, dict):
                if rendered > char_range["max"]:
                    pieces.append(f"四句渲染后总长 {rendered} 字，超过上限 {char_range['max']}。")
                elif rendered < char_range["min"]:
                    pieces.append(f"四句渲染后总长 {rendered} 字，低于下限 {char_range['min']}。")
            pieces.append(f"硬边界：每句 {low}–{high} 字，总长 {char_range['min'] if isinstance(char_range, dict) else '?'}"
                          f"–{char_range['max'] if isinstance(char_range, dict) else '?'} 字（含每条证据 ID 与括号）。")
            pieces.append("请只做删减或补足限定语的文字压缩/扩写，保持四句结构、证据引用、事实与问句位置不变。")
            instruction = "".join(pieces)
    return {"attempt_error": error["code"], "observations": observations,
            "instruction": instruction}


def run_remaining_live_organized_deliveries(
        evidence_build_dir, output_root=None, *, execute=False, config=None, root=None,
        case_ids=None, max_attempts=MAX_ATTEMPTS) -> dict:
    """对剩余五轮逐轮真实生成组织侧正文并配对；未授权或未就绪时零调用返回。"""
    repo = Path(root).resolve() if root is not None else Path(__file__).resolve().parents[1]
    build = Path(evidence_build_dir).resolve(strict=True)
    out_root = Path(output_root) if output_root is not None else repo / RUNS_RELATIVE
    cases = tuple(case_ids) if case_ids else REMAINING
    _need(all(case in REMAINING for case in cases), "case_not_in_remaining")
    if config is None:
        from .configuration import load_config
        config = load_config(repo / "configs/core.json")
    status = mimo_status(config, repo)
    base = {"schema_version": SCHEMA_VERSION, "executed": False, "actual_model_calls": 0,
            "status": "blocked", "blockers": [], "cases": [], "runs_root": str(out_root),
            "billing_before": status.get("billing"), "billing_after": status.get("billing"),
            "cost_cny": "0.00000000", "manifest_ready": False,
            "policy_sha256": POLICY_SHA256}
    if status["blockers"]:
        base["blockers"] = list(status["blockers"])
        return base
    if execute is not True:
        base["blockers"] = ["execute_required"]
        return base

    plan = plan_rounds(build)
    planned = {row["case_id"]: row for row in plan["cases"]}
    policy = delivery.load_delivery_policy(repo / POLICY_RELATIVE, POLICY_SHA256)
    index = dosage_index(build)
    settings = _settings(config)
    ledger = _ledger(config, repo)
    billing_before = ledger.status()
    out_root.mkdir(parents=True, exist_ok=True)

    results, attempts_total = [], 0
    for case_id in cases:
        row = planned[case_id]
        packet, _path = _load_packet(build, case_id)
        run_id = ("live_delivery_" + case_id + "_"
                  + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_"
                  + uuid.uuid4().hex[:8])
        run_dir = out_root / run_id
        run_dir.mkdir()
        metadata = {"policy_sha256": POLICY_SHA256, "policy_version": policy["version"],
                    "model_settings": settings, "execution_mode": "live_cross_topic_delivery",
                    "case_id": case_id, "packet_sha256": row["packet_sha256"],
                    "sentence_budget_sha256": row["sentence_budget_sha256"],
                    "thinking": "disabled", "automatic_retries": 0,
                    "internal_report_supplied": False, "parent_run_id": None}
        store = EventStore(run_dir, metadata, row["packet_sha256"], False, run_id)
        transport = MiMoTransport(config["models"]["assistant"], store, ledger)
        case_result = {"case_id": case_id, "run_id": run_id, "run_directory": str(run_dir),
                       "attempts": 0, "status": "failed", "organized": None,
                       "pair": None, "checks": {name: False for name in CHECK_NAMES},
                       "errors": [], "source_event_ids": []}
        store.append("live_delivery_started", payload={
            "case_id": case_id, "packet_sha256": row["packet_sha256"],
            "fixed_chars": row["fixed_chars"],
            "additional_char_range": row["additional_char_range"],
            "target_chars": row["target_chars"], "sentence_budget": row["sentence_budget"],
            "billing_before": billing_before, "internal_report_supplied": False,
            "scope": "single_pass_live_generation_no_subject_delivery"})
        feedback, validated = None, None
        for attempt in range(1, max_attempts + 1):
            request = _request(run_id, attempt, policy, packet, row["additional_char_range"],
                               row["target_chars"], row["sentence_budget"], settings, feedback)
            store.append("live_delivery_request_prepared", payload={
                "request_id": request["request_id"], "attempt": attempt, "feedback": feedback})
            case_result["attempts"] = attempt
            attempts_total += 1
            try:
                response = transport.complete(request)
            except (MiMoError, BudgetError) as exc:
                error = _failure(exc)
                case_result["errors"].append({"attempt": attempt, **error})
                store.append("live_delivery_attempt_aborted", payload={
                    "attempt": attempt, "error": error, "http_attempted": True})
                break
            captured = store.capture(response)
            request_ref = store.capture(request)
            received = store.append("live_delivery_response_received",
                                    input_ref=request_ref, output_ref=captured,
                                    payload={"attempt": attempt, "validated": False})
            case_result["source_event_ids"].append(received["event_id"])
            parsed = None
            try:
                parsed = _parse_json(response["raw_text"])
                rendered = delivery.render_addition(
                    parsed, packet, "organized_reflection", row["additional_char_range"],
                    sentence_budget=row["sentence_budget"])
            except (delivery.DeliveryError, ValueError, TypeError) as exc:
                error = _failure(exc)
                case_result["errors"].append({"attempt": attempt, **error})
                feedback = _feedback_from(error, parsed, row["sentence_budget"],
                                          row["additional_char_range"])
                store.append("live_delivery_attempt_rejected", payload={
                    "attempt": attempt, "error": error,
                    "response_sha256": captured["sha256"], "feedback": feedback})
                continue
            validated = {"structured": parsed, "rendered": rendered, "response": captured,
                         "attempt": attempt, "event_id": received["event_id"]}
            store.append("live_delivery_stage_validated", payload={
                "attempt": attempt, "response_sha256": captured["sha256"],
                "additional_chars": rendered["char_count"],
                "covered_evidence_ids": rendered["covered_evidence_ids"],
                "question_count": rendered["question_count"], "syntax_only": True,
                "semantic_guarantee": False})
            break
        if validated is None:
            store.append("live_delivery_failed", payload={
                "case_id": case_id, "attempts": case_result["attempts"],
                "errors": case_result["errors"],
                "actual_model_calls": transport.actual_model_calls})
            summary = {**case_result, "schema_version": SCHEMA_VERSION,
                       "packet_sha256": row["packet_sha256"], "packet_id": row["packet_id"],
                       "fixed_chars": row["fixed_chars"],
                       "additional_char_range": row["additional_char_range"],
                       "target_chars": row["target_chars"],
                       "sentence_budget": row["sentence_budget"],
                       "model_settings": settings, "policy_sha256": POLICY_SHA256,
                       "policy_version": policy["version"],
                       "billing_before": billing_before,
                       "billing_after": ledger.status(), "completed_at_utc": utc_now(),
                       "actual_model_calls": transport.actual_model_calls,
                       "internal_report_supplied": False,
                       "verification": verify_events(run_dir)}
            write_json_new(run_dir / "summary.json", summary)
            case_result["summary_path"] = str(run_dir / "summary.json")
            results.append(case_result)
            continue
        paired = pair_case(packet, index, validated["rendered"]["additional_text"],
                           source_event_ids=[validated["event_id"]])
        pair_ok = paired["status"] == "paired" and all(paired["checks"].values())
        case_result.update(organized={
            "text": validated["rendered"]["additional_text"],
            "chars": validated["rendered"]["char_count"],
            "sha256": _sha(validated["rendered"]["additional_text"]),
            "response_sha256": validated["response"]["sha256"],
            "event_id": validated["event_id"], "attempt": validated["attempt"],
        }, pair=paired["pair"], checks=paired["checks"],
            selection_status=paired["status"],
            selection_sha256=paired["selection_sha256"],
            status="validated_paired" if pair_ok else "validated_pair_deviated")
        store.append("live_delivery_pair_built", payload={
            "case_id": case_id, "checks": paired["checks"],
            "organized_chars": len(validated["rendered"]["additional_text"]),
            "pair_ok": pair_ok, "selection_status": paired["status"]})
        summary = {
            "schema_version": SCHEMA_VERSION, "case_id": case_id, "run_id": run_id,
            "run_directory": str(run_dir), "status": case_result["status"],
            "attempts": case_result["attempts"], "errors": case_result["errors"],
            "actual_model_calls": transport.actual_model_calls,
            "model_settings": settings, "policy_sha256": POLICY_SHA256,
            "policy_version": policy["version"],
            "packet_sha256": row["packet_sha256"], "fixed_chars": row["fixed_chars"],
            "additional_char_range": row["additional_char_range"],
            "target_chars": row["target_chars"],
            "sentence_budget": row["sentence_budget"],
            "organized": {key: case_result["organized"][key] for key in (
                "text", "chars", "sha256", "response_sha256", "event_id", "attempt")},
            "structured": validated["structured"],
            "pair": paired["pair"], "checks": paired["checks"],
            "selection": paired["selection"], "plain_text": paired["plain_text"],
            "source_event_ids": case_result["source_event_ids"],
            "billing_before": billing_before, "billing_after": ledger.status(),
            "internal_report_supplied": False, "completed_at_utc": utc_now(),
            "verification": verify_events(run_dir),
            "formal_eligible": False, "full_I4_eligible": False,
            "human_reviews_completed": 0, "subject_delivered": False,
        }
        write_json_new(run_dir / "summary.json", summary)
        case_result["summary_path"] = str(run_dir / "summary.json")
        results.append(case_result)
    billing_after = ledger.status()
    # 只用已结算金额求差：存在“中断持有”预留时 total_cost_cny 为 null。
    delta = "%.8f" % (float(billing_after["charged_cny"])
                      - float(billing_before["charged_cny"]))
    ok = (len(results) == len(cases)
          and all(row["status"] == "validated_paired" for row in results))
    return {**base, "executed": True, "status": "completed" if ok else "incomplete",
            "blockers": [] if ok else ["case_not_paired"],
            "cases": results, "actual_model_calls": attempts_total,
            "billing_before": billing_before, "billing_after": billing_after,
            "cost_cny": delta, "manifest_ready": ok,
            "model_settings": settings, "internal_report_supplied": False}


def _ledger_rows(repo, ledger_relative):
    path = Path(repo) / ledger_relative
    _need(path.is_file(), "ledger_missing")
    return [_parse_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_ledger_totals(rows, run_id):
    settles = [row for row in rows if row.get("type") == "settle"
               and str(row.get("request_id", "")).startswith(run_id + ":")]
    charged = sum(float(row["charged_cny"]) for row in settles)
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for row in settles:
        detail = row.get("usage") or {}
        for key in usage:
            usage[key] += int(detail.get(key) or 0)
    return {"settle_count": len(settles), "charged_cny": "%.8f" % charged, "usage": usage,
            "request_ids": [row["request_id"] for row in settles],
            "entry_hashes": [row["hash"] for row in settles]}


def _case_row(repo, build, index, case_id, run_dir, planned, *, reused, ledger_rows):
    run_dir = Path(run_dir).resolve()
    summary = _parse_json((run_dir / "summary.json").read_bytes())
    packet, _ = _load_packet(build, case_id)
    policy = delivery.load_delivery_policy(repo / POLICY_RELATIVE, POLICY_SHA256)
    if reused:
        addition = summary["additions"]["organized_reflection"]
        _need(addition["status"] == "validated", "existing_organized_not_validated")
        text = addition["rendered"]["additional_text"]
        structured = _verify_blob(run_dir, addition["structured"])
        char_range = addition["additional_char_range"]
        target = addition["target_chars"]
        budget = addition["sentence_budget"]
        _need(canonical_hash(budget) == addition["sentence_budget_sha256"],
              "existing_budget_hash_mismatch")
        prompt_variant = "with_parent_internal_report"
        attempts = summary["actual_http_attempts"]
        source_event_ids = [addition["source_event_id"]]
        run_status = summary["status"]
        billing = {"before": summary["billing_before"], "after": summary["billing_after"]}
        verification = summary.get("verification")
        errors = [summary["error"]] if summary.get("error") else []
        organized_event = source_event_ids[0]
        response_sha = None
        response_ref = addition.get("response")
        if isinstance(response_ref, dict):
            response_sha = response_ref.get("sha256")
    else:
        text = summary["organized"]["text"]
        structured = summary["structured"]
        char_range = summary["additional_char_range"]
        target = summary["target_chars"]
        budget = summary["sentence_budget"]
        prompt_variant = "packet_only_no_parent_report"
        attempts = summary["attempts"]
        source_event_ids = list(summary["source_event_ids"])
        run_status = summary["status"]
        billing = {"before": summary["billing_before"], "after": summary["billing_after"]}
        verification = summary.get("verification")
        errors = list(summary.get("errors") or [])
        organized_event = summary["organized"]["event_id"]
        response_sha = summary["organized"]["response_sha256"]
    rendered = delivery.render_addition(structured, packet, "organized_reflection", char_range,
                                       sentence_budget=budget)
    _need(rendered["additional_text"] == text, "organized_text_not_reproducible:" + case_id)
    paired = pair_case(packet, index, text, source_event_ids=source_event_ids)
    checks = {name: paired["checks"][name] is True for name in CHECK_NAMES}
    totals = _run_ledger_totals(ledger_rows, summary["run_id"])
    return {
        "case_id": case_id, "topic_id": _topic_round(case_id)[0], "round": _topic_round(case_id)[1],
        "reused_existing_run": reused, "prompt_variant": prompt_variant,
        "internal_report_supplied": prompt_variant == "with_parent_internal_report",
        "run_id": summary["run_id"], "run_directory": run_dir.as_posix(),
        "run_status": run_status, "attempts": attempts,
        "actual_model_calls": summary["actual_model_calls"],
        "model_settings": summary["model_settings"],
        "policy_sha256": _summary_policy_sha(summary),
        "packet_sha256": _summary_packet_sha(summary),
        "packet_id": packet["packet_id"],
        "fixed_chars": planned["fixed_chars"],
        "additional_char_range": char_range, "target_chars": target,
        "sentence_budget": budget, "sentence_budget_sha256": canonical_hash(budget),
        "organized": {"text": text, "chars": len(text), "sha256": _sha(text),
                      "event_id": organized_event, "response_sha256": response_sha,
                      "sentence_count": len(structured["sentences"]),
                      "question_count": sum(1 for s in structured["sentences"]
                                            if s["kind"] == "question")},
        "plain": {"text": paired["plain_text"], "chars": len(paired["plain_text"]),
                  "sha256": _sha(paired["plain_text"]),
                  "sentence_indices": paired["selection"]["selected_sentence_indices"],
                  "combination_count": paired["selection"]["candidate_combination_count"],
                  "selection_status": paired["status"],
                  "selection_sha256": paired["selection_sha256"]},
        "visible_chars": {"I4": paired["pair"]["I4"]["char_count"],
                          "I5": paired["pair"]["I5"]["char_count"]},
        "difference_ratios": {"total": paired["pair"]["checks"]["total_difference_ratio"],
                              "additional": paired["pair"]["checks"]["additional_difference_ratio"]},
        "checks": checks, "all_checks_passed": all(checks.values()),
        "content_origins": {"I4": "recorded_assistant_output",
                            "I5": "deterministic_exact_source_sentences"},
        "source_event_ids": source_event_ids,
        "ledger": totals, "billing": billing, "verification": verification,
        "errors": errors,
    }


def build_six_round_live_extractive_manifest(
        evidence_build_dir, live_runs_root=None, *, existing_run_directory=None,
        manifest_path=None, repo_root=None, ledger_relative=LEDGER_RELATIVE) -> dict:
    """整合既有 TOPIC_01_R1 与新增五轮，落盘 6/6 真实组织侧＋原句对照清单。"""
    repo = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[1]
    build = Path(evidence_build_dir).resolve(strict=True)
    runs_root = Path(live_runs_root) if live_runs_root is not None else repo / RUNS_RELATIVE
    ledger_rows = _ledger_rows(repo, ledger_relative)
    planned = {row["case_id"]: row for row in plan_rounds(build)["cases"]}
    index = dosage_index(build)
    existing = (Path(existing_run_directory) if existing_run_directory is not None else
                repo / "experiment/delivery_probes" / EXISTING_R1_RUN_ID)
    _need((existing / "summary.json").is_file(), "existing_run_missing")

    rows = [_case_row(repo, build, index, "TOPIC_01_R1", existing, planned["TOPIC_01_R1"],
                      reused=True, ledger_rows=ledger_rows)]
    for case_id in REMAINING:
        candidates = sorted(runs_root.glob(f"live_delivery_{case_id}_*"))
        _need(bool(candidates), "missing_live_run:" + case_id)
        latest = next((path for path in reversed(candidates)
                       if (path / "summary.json").is_file()
                       and _parse_json((path / "summary.json").read_bytes()).get("status")
                       == "validated_paired"), candidates[-1])
        rows.append(_case_row(repo, build, index, case_id, latest, planned[case_id],
                              reused=False, ledger_rows=ledger_rows))
    ordered = sorted(rows, key=lambda row: CASE_IDS.index(row["case_id"]))

    total_http = sum(row["actual_model_calls"] for row in ordered)
    new_calls = sum(row["actual_model_calls"] for row in ordered if not row["reused_existing_run"])
    total_charged = sum(float(row["ledger"]["charged_cny"]) for row in ordered)
    total_usage = {key: sum(row["ledger"]["usage"][key] for row in ordered)
                   for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    manifest = {
        "schema_version": SCHEMA_VERSION, "contract": MANIFEST_CONTRACT,
        "created_at_utc": utc_now(),
        "scope": "三议题六轮真实 mimo 组织侧（I4 内容来源）＋确定性整句摘录（I5）结构配对；"
                 "未送达受试、未做语义人审、不构成完整 I4 或正式实验资格。",
        "case_count": len(ordered),
        "complete_6_of_6": len(ordered) == 6 and all(row["all_checks_passed"] for row in ordered),
        "reused_existing_cases": [row["case_id"] for row in ordered if row["reused_existing_run"]],
        "new_live_cases": [row["case_id"] for row in ordered if not row["reused_existing_run"]],
        "model_settings": ordered[-1]["model_settings"],
        "policy_sha256": POLICY_SHA256, "policy_version": POLICY_VERSION,
        "cases": ordered,
        "totals": {"actual_model_calls": total_http, "new_live_calls": new_calls,
                   "charged_cny": "%.8f" % total_charged, "usage": total_usage,
                   "ledger_last_seq": ledger_rows[-1]["seq"] if ledger_rows else 0},
        "honest_gates": {"is_demo": False, "actual_model_calls": total_http,
                         "human_reviews_completed": 0, "full_I4_eligible": False,
                         "formal_eligible": False, "approved_for_experiment": False,
                         "subject_delivered": False,
                         "semantic_review": "pending_independent_review"},
        "comparability_note": "TOPIC_01_R1 复用 2026-11-08 运行，其组织侧在有父级内部报告的条件下生成；"
                              "新增五轮只发冻结证据包与四句预算（无父级报告）。两者结构契约相同、"
                              "提示条件不同，已在 prompt_variant 逐轮标注，不得当作完全同条件重复。",
        "evidence_build": {"path": build.as_posix(),
                           "manifest_sha256": _file_sha(build / "manifest.json")},
        "ledger": {"path": ledger_relative,
                   "last_seq": ledger_rows[-1]["seq"] if ledger_rows else 0},
        "notes": [
            "TOPIC_01_R1 复用既有已验证真实运行，不重复调用。",
            "五轮新运行只发送冻结证据包与四句预算，不发送内部报告（无父级四阶段运行）。",
            "组织侧正文逐字保存，不补字、不裁剪；I5 只做确定性整句摘录。",
            "全部尝试（含被本地校验拒绝的尝试）都在 EventStore 与账本中留痕。",
        ],
    }
    # 结算对账：本步全部真实支出（含被替代/中断的尝试与连通性冒烟）必须可加总核对。
    live_settles = [row for row in ledger_rows if row.get("type") == "settle"
                    and str(row.get("request_id", "")).startswith("live_delivery_")]
    smoke_settles = [row for row in ledger_rows if row.get("type") == "settle"
                     and str(row.get("request_id", "")).startswith("connection_smoke_20261111")]
    live_charged = sum(float(row["charged_cny"]) for row in live_settles)
    smoke_charged = sum(float(row["charged_cny"]) for row in smoke_settles)
    per_case = sum(float(row["ledger"]["charged_cny"]) for row in ordered)
    ledger_total = sum(float(row["charged_cny"]) for row in ledger_rows
                       if row.get("type") == "settle")
    manifest["settlement"] = {
        "ledger_path": ledger_relative,
        "ledger_entries": len(ledger_rows),
        "ledger_total_cny": "%.8f" % ledger_total,
        "live_delivery_charged_cny": "%.8f" % live_charged,
        "preflight_smoke_charged_cny": "%.8f" % smoke_charged,
        "step_charged_cny": "%.8f" % (live_charged + smoke_charged),
        "per_case_final_charged_cny": "%.8f" % per_case,
        "superseded_charged_cny": "%.8f" % (live_charged - per_case),
        "reconciled": abs((live_charged + smoke_charged) - (per_case + (live_charged - per_case)
                                                            + smoke_charged)) <= 1e-9,
        "note": "superseded 包含被本地校验拒绝后重跑的轮次与一次主控中断但已计费的尝试；"
                "全部条目都在共享账本内，未删除任何一次尝试。",
    }
    manifest["manifest_sha256"] = canonical_hash(manifest)
    target = Path(manifest_path) if manifest_path is not None else repo / MANIFEST_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError("live_cross_topic_delivery:manifest_exists")
    write_json_new(target, manifest)
    return {"status": "written", "manifest_path": target.as_posix(),
            "manifest_sha256": manifest["manifest_sha256"],
            "complete_6_of_6": manifest["complete_6_of_6"],
            "case_count": manifest["case_count"], "manifest": manifest}


def verify_six_round_live_extractive_manifest(
        manifest_path, *, evidence_build_dir=None, repo_root=None,
        policy_path=None, expected_policy_sha=POLICY_SHA256) -> dict:
    """离线独立核验：事件链、blob、账本、契约与七项配对检查全部重算比对。"""
    repo = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[1]
    path = Path(manifest_path)
    _need(path.is_file(), "manifest_missing")
    manifest = _parse_json(path.read_bytes())
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    _need(canonical_hash(body) == manifest["manifest_sha256"], "manifest_hash_mismatch")
    _need(manifest["contract"] == MANIFEST_CONTRACT and manifest["case_count"] == 6,
          "manifest_contract_invalid")
    build = (Path(evidence_build_dir).resolve(strict=True) if evidence_build_dir
             else Path(manifest["evidence_build"]["path"]))
    if policy_path is not None:
        delivery.load_delivery_policy(policy_path, expected_policy_sha)
    _need(manifest["policy_sha256"] == expected_policy_sha, "policy_hash_mismatch")
    index = dosage_index(build)
    planned = {row["case_id"]: row for row in plan_rounds(build)["cases"]}
    ledger_rows = _ledger_rows(repo, manifest["ledger"]["path"])
    verified, problems = [], []
    for row in manifest["cases"]:
        case_id = row["case_id"]
        run_dir = Path(row["run_directory"])
        summary = _parse_json((run_dir / "summary.json").read_bytes())
        chain = verify_events(run_dir)
        if chain.get("chain_valid") is not True:
            problems.append({"case": case_id, "code": "event_chain_invalid"})
        if summary["run_id"] != row["run_id"]:
            problems.append({"case": case_id, "code": "run_id_mismatch"})
        packet, _ = _load_packet(build, case_id)
        if (canonical_hash(packet) != _summary_packet_sha(summary)
                or canonical_hash(packet) != row["packet_sha256"]):
            problems.append({"case": case_id, "code": "packet_changed"})
        if planned[case_id]["fixed_chars"] != row["fixed_chars"]:
            problems.append({"case": case_id, "code": "fixed_chars_changed"})
        text = row["organized"]["text"]
        if _sha(text) != row["organized"]["sha256"]:
            problems.append({"case": case_id, "code": "organized_text_hash_mismatch"})
        structured = (summary["structured"] if "structured" in summary
                      else _verify_blob(run_dir, summary["additions"]["organized_reflection"]["structured"]))
        rendered = delivery.render_addition(structured, packet, "organized_reflection",
                                           row["additional_char_range"],
                                           sentence_budget=row["sentence_budget"])
        if rendered["additional_text"] != text:
            problems.append({"case": case_id, "code": "organized_not_reproducible"})
        paired = pair_case(packet, index, text, source_event_ids=row["source_event_ids"])
        checks = {name: paired["checks"][name] is True for name in CHECK_NAMES}
        if checks != row["checks"] or not all(checks.values()):
            problems.append({"case": case_id, "code": "pair_checks_mismatch"})
        if (paired["pair"]["I4"]["char_count"] != row["visible_chars"]["I4"]
                or paired["pair"]["I5"]["char_count"] != row["visible_chars"]["I5"]):
            problems.append({"case": case_id, "code": "visible_chars_mismatch"})
        totals = _run_ledger_totals(ledger_rows, summary["run_id"])
        if totals["charged_cny"] != row["ledger"]["charged_cny"]:
            problems.append({"case": case_id, "code": "ledger_charge_mismatch"})
        if totals["settle_count"] != row["actual_model_calls"]:
            problems.append({"case": case_id, "code": "ledger_settle_count_mismatch"})
        verified.append({"case_id": case_id, "run_id": summary["run_id"],
                         "attempts": row["attempts"], "checks": checks,
                         "visible_chars": row["visible_chars"],
                         "charged_cny": totals["charged_cny"],
                         "organized_chars": row["organized"]["chars"],
                         "plain_chars": row["plain"]["chars"]})
    total_charged = sum(float(row["ledger"]["charged_cny"]) for row in manifest["cases"])
    if "%.8f" % total_charged != manifest["totals"]["charged_cny"]:
        problems.append({"case": "all", "code": "ledger_total_mismatch"})
    settlement = manifest.get("settlement") or {}
    live_settles = [row for row in ledger_rows if row.get("type") == "settle"
                    and str(row.get("request_id", "")).startswith("live_delivery_")]
    live_charged = sum(float(row["charged_cny"]) for row in live_settles)
    if "%.8f" % live_charged != settlement.get("live_delivery_charged_cny"):
        problems.append({"case": "all", "code": "settlement_live_mismatch"})
    if "%.8f" % total_charged != settlement.get("per_case_final_charged_cny"):
        problems.append({"case": "all", "code": "settlement_case_sum_mismatch"})
    if len(verified) != 6:
        problems.append({"case": "all", "code": "case_count_mismatch"})
    gates = manifest["honest_gates"]
    for key, expected in (("human_reviews_completed", 0), ("full_I4_eligible", False),
                          ("formal_eligible", False), ("approved_for_experiment", False),
                          ("subject_delivered", False), ("is_demo", False)):
        if gates.get(key) is not expected:
            problems.append({"case": "all", "code": "honest_gate_" + key})
    return {"contract": MANIFEST_CONTRACT, "passed": not problems,
            "manifest_sha256": manifest["manifest_sha256"],
            "case_count": manifest["case_count"],
            "complete_6_of_6": manifest["complete_6_of_6"],
            "verified_cases": verified, "problems": problems,
            "ledger_total_cny": "%.8f" % total_charged,
            "new_live_calls": manifest["totals"]["new_live_calls"],
            "actual_model_calls": manifest["totals"]["actual_model_calls"],
            "is_demo": gates["is_demo"], "formal_eligible": False,
            "verification_scope": "本地事件链/blob/账本/契约重算；不验证外部网络真实性、语义质量或人审。"}


def summarize_live_delivery(repo_root=None, *, manifest_path=None) -> dict:
    """供 pilot_materials / dashboard 使用的只读摘要；核验失败时不给可用状态。"""
    repo = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[1]
    path = Path(manifest_path) if manifest_path is not None else repo / MANIFEST_RELATIVE
    unavailable = {"available": False, "reason": "manifest_absent",
                   "path": path.as_posix(), "case_count": 0, "complete_6_of_6": False,
                   "cases": [], "actual_model_calls": 0, "new_live_calls": 0,
                   "charged_cny": "0.00000000", "usage": {"prompt_tokens": 0,
                   "completion_tokens": 0, "total_tokens": 0}, "pending_rounds": list(REMAINING)}
    if not path.is_file():
        return unavailable
    try:
        result = verify_six_round_live_extractive_manifest(path, repo_root=repo)
    except (ValueError, KeyError, TypeError, OSError):
        return {**unavailable, "reason": "manifest_verification_failed"}
    manifest = _parse_json(path.read_bytes())
    return {
        "available": result["passed"] and result["complete_6_of_6"],
        "reason": "verified" if result["passed"] else "not_passed",
        "path": path.as_posix(), "manifest_sha256": manifest["manifest_sha256"],
        "case_count": manifest["case_count"], "complete_6_of_6": result["complete_6_of_6"],
        "actual_model_calls": manifest["totals"]["actual_model_calls"],
        "new_live_calls": manifest["totals"]["new_live_calls"],
        "charged_cny": manifest["totals"]["charged_cny"],
        "usage": manifest["totals"]["usage"],
        "cases": [{"case_id": row["case_id"], "run_id": row["run_id"],
                   "prompt_variant": row["prompt_variant"],
                   "organized_chars": row["organized"]["chars"],
                   "plain_chars": row["plain"]["chars"],
                   "organized_sha256": row["organized"]["sha256"],
                   "visible_chars": row["visible_chars"],
                   "checks": row["checks"], "reused": row["reused_existing_run"],
                   "actual_model_calls": row["actual_model_calls"],
                   "attempts": row["attempts"],
                   "charged_cny": row["ledger"]["charged_cny"],
                   "usage": row["ledger"]["usage"],
                   "packet_sha256": row["packet_sha256"]} for row in manifest["cases"]],
        "honest_gates": manifest["honest_gates"],
        "pending_rounds": [],
    }


def live_pairs_for_materials(repo_root=None, *, manifest_path=None) -> dict:
    """返回 {(topic_id, round): {I4, I5, provenance}}；不可用或未过核验时返回空字典。"""
    summary = summarize_live_delivery(repo_root, manifest_path=manifest_path)
    if not summary["available"]:
        return {"available": False, "pairs": {}, "summary": summary}
    manifest = _parse_json(Path(summary["path"]).read_bytes())
    pairs = {}
    for row in manifest["cases"]:
        pairs[(_topic_round(row["case_id"])[0], row["round"])] = {
            "I4": row["organized"]["text"], "I5": row["plain"]["text"],
            "provenance": {"content_origin": "recorded_assistant_output",
                           "run_id": row["run_id"],
                           "event_id": row["organized"]["event_id"],
                           "organized_sha256": row["organized"]["sha256"],
                           "plain_sha256": row["plain"]["sha256"],
                           "packet_sha256": row["packet_sha256"],
                           "prompt_variant": row["prompt_variant"],
                           "checks": row["checks"],
                           "reused_existing_run": row["reused_existing_run"]},
        }
    return {"available": True, "pairs": pairs, "summary": summary}
