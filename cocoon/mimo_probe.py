"""MiMo连接准备/单次冒烟/显式助手探测，不开放全量实验。"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from .assistant import AssistantAdapter
from .budget import BudgetLedger, BudgetError
from .contracts import validate_packet
from .mimo import MiMoTransport, MiMoError, credential_status, load_key, _has_key, _validate_config
from .prompts import load_prompt_bundle, verify_original_sources
from .runtime import EventStore, canonical_hash, utc_now, verify_events, write_json_new, _parse_json

LEDGER_RELATIVE = "experiment/billing/mimo_initial_probe_v1.jsonl"
APPROVED_LIMIT = "5.00"


def _ledger(config, root):
    budget = config.get("budget", {})
    if (budget.get("initial_probe_authorized") is not True
            or budget.get("initial_probe_limit_cny") != APPROVED_LIMIT
            or budget.get("initial_probe_ledger") != LEDGER_RELATIVE):
        raise BudgetError("probe_authorization_required")
    return BudgetLedger(Path(root) / LEDGER_RELATIVE, APPROVED_LIMIT)


def mimo_status(config, root):
    """不联网，仅报配置、密钥存在性与持久预算；不输出密钥长度/值。"""
    blockers = []
    try:
        model = _validate_config(config["models"]["assistant"])
    except (MiMoError, KeyError):
        blockers.append("invalid_mimo_config")
        model = None
    credential = credential_status()
    if not credential["present"]:
        blockers.append("missing_MIMO_API_KEY")
    elif not credential["usable_format"]:
        blockers.append("invalid_MIMO_API_KEY")
    try:
        billing = _ledger(config, root).status()
        held = billing.get("held_interrupted_request_count", 0)
        if billing["blocked"] or billing["unresolved_request_count"] - held:
            blockers.append("billing_reconciliation_required")
        from decimal import Decimal
        from .budget import INPUT_RESERVE_TOKENS, RATE_INPUT_CNY_PER_MILLION, RATE_OUTPUT_CNY_PER_MILLION
        smoke_reserve = (Decimal(INPUT_RESERVE_TOKENS) * RATE_INPUT_CNY_PER_MILLION + Decimal(128) * RATE_OUTPUT_CNY_PER_MILLION) / Decimal(1000000)
        if Decimal(billing["available_cny"]) < smoke_reserve:
            blockers.append("insufficient_budget_for_safe_reservation")
    except BudgetError as exc:
        billing = {"error_code": exc.code}
        blockers.append("billing_" + exc.code)
    return {"status": "blocked" if blockers else "ready_for_explicit_smoke",
            "provider": "xiaomi_mimo", "model": model["model"] if model else None,
            "endpoint": model["endpoint"] if model else None, "credential": credential,
            "billing": billing, "blockers": blockers, "network_called": False,
            "live_connection_verified": False, "formal_launch_enabled": False,
            "ready_scope": "仅配置检查；不代表密钥有效或已测通模型",
            "parameters": {key: model[key] for key in ("temperature", "max_tokens", "thinking", "timeout_seconds")} if model else None}


def _run_dir(root, kind):
    parent = Path(root) / "experiment/mimo_probes"
    if not parent.is_dir():
        raise FileNotFoundError("mimo_probes父目录不存在")
    run_id = kind + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = parent / run_id
    out.mkdir()
    return out, run_id


def _source_check(config):
    a = config["assistant"]
    bundle = load_prompt_bundle(a["prompt_bundle_path"], a["prompt_bundle_sha256"])
    verify_original_sources(bundle, a["entry_path"], a["source_path"])


def run_probe(config, root, *, execute=False, packet_path=None, user_view_path=None):
    """无--execute只返回状态。真实assistant仅读取明确提供的packet，绝不自动挑样本。"""
    state = mimo_status(config, root)
    if execute is not True or state["blockers"]:
        return {**state, "executed": False, "actual_model_calls": 0,
                "next_step": "在本机配置MIMO_API_KEY后显式执行mimo-smoke --execute；不会自动发起请求。"}
    _source_check(config)
    model = config["models"]["assistant"]
    settings = {key: model[key] for key in ("provider", "model", "temperature", "max_tokens")}
    key = load_key()
    view = None
    if packet_path is not None:
        packet_bytes = Path(packet_path).read_bytes()
        if len(packet_bytes) > 2000000:
            raise ValueError("证据包文件过大")
        packet = _parse_json(packet_bytes)
        if _has_key(packet, key):
            raise MiMoError("credential_in_request")
        packet = validate_packet(packet, allow_demo=False)
        if user_view_path is not None:
            view = Path(user_view_path).read_text(encoding="utf-8")
            if key in view:
                raise MiMoError("credential_in_request")
        kind = "assistant_probe"
        material = packet
    else:
        if user_view_path is not None:
            raise ValueError("用户观点文件只能与明确证据包一起使用")
        kind = "connection_smoke"
        material = {"purpose": "connection_test_only", "research_data": False,
                    "prompt": "请只返回一个JSON对象：{\"connection\":\"ok\"}。"}
        settings["max_tokens"] = 128
    out, run_id = _run_dir(root, kind)
    ledger = _ledger(config, root)
    store = EventStore(out, {"model_settings": settings, "execution_mode": "live_probe",
                       "test_kind": kind, "thinking": "disabled", "initial_budget_cny": APPROVED_LIMIT},
                       canonical_hash(material), False, run_id)
    transport = MiMoTransport(model, store, ledger)
    store.append("probe_started", payload={"scope": kind, "formal_eligible": False,
                 "billing_before": ledger.status(), "source_type": "explicit_packet" if packet_path else "connection_test_only"})
    result = None
    try:
        if packet_path is None:
            request = {"request_id": run_id + ":smoke:1", "stage": "connection_smoke", "is_demo": False,
                       "model_settings": settings, "messages": [
                           {"role": "system", "content": "仅执行接口连通性测试，不进行检索或研究；返回要求的JSON。"},
                           {"role": "user", "content": material["prompt"]}]}
            response = transport.complete(request)
            parsed = _parse_json(response["raw_text"])
            result = {"status": "succeeded" if parsed == {"connection": "ok"} else "unexpected_output",
                      "response": response, "exact_output_check": parsed == {"connection": "ok"}}
        else:
            a = config["assistant"]
            adapter = AssistantAdapter(store, a["prompt_bundle_path"], a["prompt_bundle_sha256"],
                                       settings, transport, execution_mode="live_probe")
            result = adapter.run(packet, run_id + ":assistant", include_mirror=view is not None, user_view=view)
    except (MiMoError, BudgetError) as exc:
        result = {"status": "failed", "error_code": exc.code,
                  "details": exc.details if isinstance(exc, MiMoError) else None}
    except (ValueError, TypeError, OSError):
        result = {"status": "failed", "error_code": "probe_validation_or_io_error"}
    try:
        billing = ledger.status()
    except BudgetError as exc:
        billing = {"error_code": exc.code, "total_cost_cny": None}
    result.update(run_id=run_id, run_directory=str(out), kind=kind, executed=True,
                  actual_model_calls=transport.actual_model_calls, is_demo=False,
                  formal_eligible=False, demo_fallback=False, completed_at_utc=utc_now(),
                  billing_cumulative=billing, model_call_count_basis="http_attempts_not_billing_confirmation")
    store.append("probe_completed" if result["status"] == "succeeded" else "probe_failed", payload=result,
                 status=result["status"])
    result["verification"] = verify_events(out)
    write_json_new(out / "summary.json", result)
    return result
