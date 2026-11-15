"""MiMo 单次在线探测传输；无重试、回退、工具或自动检索。

actual_model_calls 只计 opener.open 的尝试次数，不代表已计费次数。
预算接口由调用方注入；reserve/settle 的 BudgetError 原样交给控制器。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request

from .budget import BudgetError, BudgetLedger
from .runtime import EventStore, _parse_json, _reject_sensitive_keys


MIMO_ENDPOINT = "https://api.xiaomimimo.com/v1/chat/completions"
MIMO_MODEL = "mimo-v2.6-flash"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_CONTEXT_BYTES = 250000
_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
_ERROR_MESSAGES = {
    "missing_credential": "未设置所需的凭据环境变量。",
    "invalid_credential": "凭据或凭据环境变量名称格式无效。",
    "invalid_config": "模型配置不符合白名单。",
    "forbidden_endpoint": "只允许已确认的官方接口地址。",
    "live_disabled": "未明确启用在线探测。",
    "mode_mismatch": "在线探测不接受演示标记。",
    "invalid_request": "请求字段或模型参数不符合协议。",
    "credential_in_request": "请求包含当前凭据，已阻止保存和发送。",
    "context_too_large": "消息的 UTF-8 字节总数超限，未发送请求。",
    "audit_error": "审计记录未能安全保存，已停止。",
    "timeout": "单次请求超时，未重试，计费状态可能不确定。",
    "connection_error": "连接失败，未重试，计费状态可能不确定。",
    "transport_error": "传输失败，未重试，未记录异常原文。",
    "auth_failed": "上游返回鉴权失败。",
    "forbidden": "上游拒绝访问。",
    "rate_limited": "上游返回限流，未重试。",
    "upstream_error": "上游服务错误，未重试。",
    "redirect_blocked": "已阻止重定向，未向新地址发送凭据。",
    "http_error": "上游返回 HTTP 错误。",
    "response_too_large": "响应超过字节上限，未继续读取。",
    "response_not_recordable": "响应含凭据或敏感字段，正文未持久化。",
    "invalid_encoding": "响应不是有效 UTF-8 文本。",
    "invalid_json": "响应不是有效的严格 JSON。",
    "missing_usage": "响应缺少用量，保留预算预留并停止。",
    "invalid_usage": "响应的用量无效，保留预算预留并停止。",
    "unresolved_attempt": "此前调用的用量不确定，此传输实例已停止。",
    "invalid_response": "响应信封不符合协议。",
    "model_mismatch": "响应模型不是已确认的 MiMo 模型或其修订版。",
    "refused": "模型拒绝输出或触发内容过滤。",
    "unsupported_tool_call": "模型返回未配置的工具调用，已停止。",
    "output_truncated": "模型输出被截断，已停止。",
}
MIMO_ERROR_CODES = frozenset(_ERROR_MESSAGES)


class MiMoError(Exception):
    """错误消息固定；details 只保留受限类型的安全审计元数据。"""

    def __init__(self, code, details=None):
        self.code = code if type(code) is str and code in MIMO_ERROR_CODES else "transport_error"
        self.details = {}
        if type(details) is dict:
            for name, value in details.items():
                if name == "status" and type(value) is int and 100 <= value <= 599:
                    self.details[name] = value
                elif name == "response_bytes" and type(value) is int and value >= 0:
                    self.details[name] = value
                elif name == "response_sha256" and type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value):
                    self.details[name] = value
                elif name == "omitted_for_credentials" and type(value) is bool:
                    self.details[name] = value
        super().__init__(_ERROR_MESSAGES[self.code])


def _usable_key(value):
    return (type(value) is str and value.startswith("sk-") and len(value) > 3
            and value.isascii() and all(33 <= ord(char) <= 126 for char in value))


def load_key(credential_env="MIMO_API_KEY") -> str:
    if type(credential_env) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", credential_env):
        raise MiMoError("invalid_credential")
    value = os.environ.get(credential_env)
    if value is None:
        raise MiMoError("missing_credential")
    if not _usable_key(value):
        raise MiMoError("invalid_credential")
    return value


def credential_status() -> dict:
    value = os.environ.get("MIMO_API_KEY")
    return {"env": "MIMO_API_KEY", "present": value is not None, "usable_format": _usable_key(value)}


def _validate_config(config):
    fields = {"provider", "model", "endpoint", "credential_env", "temperature",
              "max_tokens", "thinking", "timeout_seconds", "live_enabled"}
    if type(config) is not dict or set(config) != fields:
        raise MiMoError("invalid_config")
    if config["endpoint"] != MIMO_ENDPOINT:
        raise MiMoError("forbidden_endpoint")
    if config["live_enabled"] is not True:
        raise MiMoError("live_disabled")
    if (config["provider"] != "xiaomi_mimo" or config["model"] != MIMO_MODEL
            or config["thinking"] != "disabled" or config["credential_env"] != "MIMO_API_KEY"):
        raise MiMoError("invalid_config")
    temperature = config["temperature"]
    if type(temperature) not in (int, float) or not 0 <= temperature <= 1.5:
        raise MiMoError("invalid_config")
    if type(config["max_tokens"]) is not int or not 1 <= config["max_tokens"] <= 131072:
        raise MiMoError("invalid_config")
    timeout = config["timeout_seconds"]
    try:
        valid_timeout = type(timeout) in (int, float) and timeout > 0 and math.isfinite(timeout)
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise MiMoError("invalid_config")
    return dict(config)


def _wire_body(request, config, key):
    fields = {"request_id", "stage", "model_settings", "messages", "is_demo"}
    if type(request) is not dict or set(request) != fields:
        raise MiMoError("invalid_request")
    if request["is_demo"] is not False:
        raise MiMoError("mode_mismatch")
    for name in ("request_id", "stage"):
        if type(request[name]) is not str or not request[name].strip():
            raise MiMoError("invalid_request")
        if key in request[name]:
            raise MiMoError("credential_in_request")
        try:
            request[name].encode("utf-8")
        except UnicodeError:
            raise MiMoError("invalid_request") from None
    settings = request["model_settings"]
    if type(settings) is not dict or set(settings) != {"provider", "model", "temperature", "max_tokens"}:
        raise MiMoError("invalid_request")
    if (settings["provider"] != config["provider"] or settings["model"] != config["model"]
            or type(settings["temperature"]) not in (int, float)
            or settings["temperature"] != config["temperature"]
            or type(settings["max_tokens"]) is not int
            or not 1 <= settings["max_tokens"] <= config["max_tokens"]):
        raise MiMoError("invalid_request")
    messages = request["messages"]
    if type(messages) is not list or not 1 <= len(messages) <= 2:
        raise MiMoError("invalid_request")
    copied = []
    size = 0
    for message in messages:
        if (type(message) is not dict or set(message) != {"role", "content"}
                or message["role"] not in ("system", "user") or type(message["content"]) is not str):
            raise MiMoError("invalid_request")
        content = message["content"]
        if key in content:
            raise MiMoError("credential_in_request")
        try:
            size += len(content.encode("utf-8"))
        except UnicodeError:
            raise MiMoError("invalid_request") from None
        copied.append(dict(message))
    if size > _MAX_CONTEXT_BYTES:
        raise MiMoError("context_too_large")
    # 只负责文本传输；严格 JSON、引用与无 HTML 的输出契约仍由助手提示词及校验器负责。
    return {"model": config["model"], "messages": copied,
            "max_completion_tokens": settings["max_tokens"], "thinking": {"type": "disabled"},
            "temperature": config["temperature"], "stream": False}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, "重定向已拒绝。", headers, fp)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def _http_code(status):
    if status == 401:
        return "auth_failed"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    if 500 <= status <= 599:
        return "upstream_error"
    if 300 <= status <= 399:
        return "redirect_blocked"
    return "http_error"


def _safe_headers(headers, key):
    result = {}
    for name in ("request-id", "x-request-id"):
        value = headers.get(name) if headers is not None else None
        if (type(value) is str and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", value)
                and key not in value and "sk-" not in value):
            result[name] = value
    return result


def _has_key(value, key):
    if isinstance(value, str):
        # 检查嵌套JSON的Unicode转义形式；只做安全检测，不改写待留存原文。
        candidate = value
        for _ in range(6):
            if key in candidate:
                return True
            decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match[1], 16)), candidate)
            if decoded == candidate:
                return False
            candidate = decoded
        return key in candidate
    if isinstance(value, dict):
        return any(key in name or _has_key(item, key) for name, item in value.items())
    if isinstance(value, list):
        return any(_has_key(item, key) for item in value)
    return False


def _inspect_body(data, key):
    """先扫描可解析信封（包含被重复字段遮蔽的对象），再严格解析。"""
    if key.encode("ascii") in data:
        return None, None, "response_not_recordable"
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return None, None, "invalid_encoding"
    sensitive = False

    def inspect_object(pairs):
        nonlocal sensitive
        for name, value in pairs:
            item = {name: value}
            try:
                _reject_sensitive_keys(item)
            except ValueError:
                sensitive = True
            if _has_key(item, key):
                sensitive = True
        return dict(pairs)

    try:
        inspected = json.loads(text, object_pairs_hook=inspect_object)
        sensitive = sensitive or _has_key(inspected, key)
    except RecursionError:
        # 未完成敏感字段扫描时不能宣称正文可安全持久化。
        return None, None, "response_not_recordable"
    except ValueError:
        # 对不完整信封无法证明敏感字段检查已完成，保留hash/字节数，不留正文。
        return None, None, "invalid_json"
    if sensitive:
        return None, None, "response_not_recordable"
    try:
        return text, _parse_json(text), None
    except (ValueError, RecursionError):
        return None, None, "invalid_json"


def _usage(envelope):
    value = envelope.get("usage") if type(envelope) is dict else None
    if value is None:
        return None, "missing_usage"
    if (type(value) is not dict or any(name not in value for name in _USAGE_FIELDS)
            or any(type(value[name]) is not int or value[name] < 0 for name in _USAGE_FIELDS)
            or value["total_tokens"] != value["prompt_tokens"] + value["completion_tokens"]):
        return None, "invalid_usage"
    return value, None


def _normalise(envelope, usage):
    if type(envelope) is not dict or envelope.get("object") != "chat.completion":
        raise MiMoError("invalid_response")
    for name in ("id", "model"):
        if type(envelope.get(name)) is not str or not envelope[name].strip():
            raise MiMoError("invalid_response")
    model = envelope["model"]
    if model != MIMO_MODEL and not model.startswith(MIMO_MODEL + "-"):
        raise MiMoError("model_mismatch")
    choices = envelope.get("choices")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise MiMoError("invalid_response")
    choice = choices[0]
    message = choice.get("message")
    if type(message) is not dict or message.get("role") != "assistant":
        raise MiMoError("invalid_response")
    finish = choice.get("finish_reason")
    if finish in ("length", "repetition_truncation"):
        raise MiMoError("output_truncated")
    if finish in ("content_filter", "refusal") or message.get("refusal"):
        raise MiMoError("refused")
    if finish in ("tool_calls", "function_call") or message.get("tool_calls") or message.get("function_call"):
        raise MiMoError("unsupported_tool_call")
    if finish != "stop" or type(message.get("content")) is not str:
        raise MiMoError("invalid_response")
    return {"raw_text": message["content"], "provider": "xiaomi_mimo", "model": model,
            "response_id": envelope["id"], "usage": {name: usage[name] for name in _USAGE_FIELDS},
            "finish_reason": "stop", "is_demo": False}


class MiMoTransport:
    execution_mode = "live_probe"

    def __init__(self, config: dict, store: EventStore, ledger: BudgetLedger):
        self.config = _validate_config(config)
        if not isinstance(store, EventStore) or store.is_demo is not False:
            raise MiMoError("mode_mismatch")
        if any(not callable(getattr(ledger, name, None)) for name in ("reserve", "settle", "mark_uncertain", "status")):
            raise MiMoError("invalid_config")
        self.store = store
        self.ledger = ledger
        self.actual_model_calls = 0
        self._halted = False

    def _mark_uncertain(self, request_id, code):
        self._halted = True
        reason = {
            "auth_failed": "http_401", "forbidden": "http_error", "rate_limited": "http_error",
            "upstream_error": "http_error", "redirect_blocked": "http_error", "http_error": "http_error",
            "timeout": "timeout", "connection_error": "connection_error", "transport_error": "network_error",
            "missing_usage": "missing_usage", "invalid_usage": "invalid_usage", "audit_error": "interrupted",
        }.get(code, "invalid_response")
        self.ledger.mark_uncertain(request_id, reason)

    def _record(self, request_id, event_type, payload, captured=None):
        try:
            fields = {}
            if captured is not None:
                descriptor = self.store.capture(captured)
                prefix = "input" if event_type == "mimo_request_prepared" else "output"
                fields[prefix + "_ref"] = descriptor["ref"]
                fields[prefix + "_sha256"] = descriptor["sha256"]
            self.store.append(event_type, task_id=request_id, actor_id="mimo_transport",
                              actor_role="transport", phase="live_probe", status="recorded",
                              payload=payload, **fields)
        except Exception:
            raise MiMoError("audit_error") from None

    def _stop(self, request_id, code, details=None, *, uncertain=True):
        error = MiMoError(code, details)
        if uncertain:
            self._mark_uncertain(request_id, error.code)
        self._record(request_id, "mimo_request_failed", {
            "code": error.code, "details": error.details, "actual_model_calls": self.actual_model_calls,
            "attempt_is_not_billing_confirmation": True,
        })
        raise error from None

    def _exchange(self, body, key, config):
        try:
            wire = urllib.request.Request(MIMO_ENDPOINT, data=body, method="POST", headers={
                "api-key": key, "Content-Type": "application/json", "Accept": "application/json",
            })
            # 不替换 TLS context，沿用标准校验及系统代理；不安装 Cookie/认证或重试处理器。
            opener = urllib.request.build_opener(NoRedirect())
            self.actual_model_calls += 1
            try:
                response = opener.open(wire, timeout=config["timeout_seconds"])
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.status
                headers = _safe_headers(response.headers, key)
                data = response.read(_MAX_RESPONSE_BYTES + 1)
            return status, headers, data
        except TimeoutError:
            raise MiMoError("timeout") from None
        except urllib.error.URLError as error:
            code = "timeout" if isinstance(error.reason, TimeoutError) else "connection_error"
            raise MiMoError(code) from None
        except OSError:
            raise MiMoError("connection_error") from None
        except Exception:
            raise MiMoError("transport_error") from None

    def complete(self, request):
        config = _validate_config(self.config)
        key = load_key(config["credential_env"])
        if self.store.is_demo is not False:
            raise MiMoError("mode_mismatch")
        wire_body = _wire_body(request, config, key)
        if self._halted:
            raise MiMoError("unresolved_attempt")
        request_id = request["request_id"]
        body = json.dumps(wire_body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._record(request_id, "mimo_request_prepared", {
            "actual_model_calls": self.actual_model_calls, "max_completion_tokens": wire_body["max_completion_tokens"],
            "attempt_is_not_billing_confirmation": True,
        }, wire_body)
        # 预算阻塞时尚未构建 opener，更不可能调用模型。BudgetError.code 由父调用方处理。
        self.ledger.reserve(request_id, wire_body["max_completion_tokens"])
        try:
            status, headers, data = self._exchange(body, key, config)
        except MiMoError as error:
            self._stop(request_id, error.code)
        details = {"status": status, "response_bytes": len(data), "response_sha256": hashlib.sha256(data).hexdigest()}
        if len(data) > _MAX_RESPONSE_BYTES:
            # 这里只是已读取前缀的哈希/字节数，不冒充完整 HTTP 正文。
            try:
                self._record(request_id, "mimo_response_omitted", dict(details, bounded_prefix=True), details)
            except MiMoError:
                self._mark_uncertain(request_id, "audit_error")
                raise
            self._stop(request_id, "response_too_large", details)
        text, envelope, parse_error = _inspect_body(data, key)
        if parse_error == "response_not_recordable":
            details["omitted_for_credentials"] = True
        if parse_error == "invalid_json":
            details["omitted_due_to_incomplete_security_scan"] = True
        captured = {"raw_http_text": text} if text is not None else dict(details)
        try:
            self._record(request_id, "mimo_response_received", {
                **details, "headers": headers, "actual_model_calls": self.actual_model_calls,
                "attempt_is_not_billing_confirmation": True,
            }, captured)
        except MiMoError:
            self._mark_uncertain(request_id, "audit_error")
            raise
        if parse_error == "response_not_recordable":
            self._stop(request_id, parse_error, details)
        raw_usage, usage_error = _usage(envelope)
        settled = False
        if usage_error is None:
            try:
                # 完整 usage 交给账本，包含缓存/推理详情；返回给助手时才投影三个字段。
                self.ledger.settle(request_id, raw_usage)
            except BudgetError as error:
                self._halted = True
                # billing_anomaly 已由账本封禁；不能再次标记而遮蔽原错误。
                if error.code == "invalid_usage":
                    self._mark_uncertain(request_id, "invalid_usage")
                raise
            except Exception:
                self._mark_uncertain(request_id, "invalid_usage")
                raise MiMoError("invalid_usage", details) from None
            settled = True
        if type(status) is not int or not 200 <= status <= 299:
            code = _http_code(status) if type(status) is int else "http_error"
            self._stop(request_id, code, details, uncertain=not settled)
        if parse_error is not None:
            self._stop(request_id, parse_error, details, uncertain=not settled)
        if usage_error is not None:
            self._stop(request_id, usage_error, details)
        try:
            result = _normalise(envelope, raw_usage)
        except MiMoError as error:
            self._stop(request_id, error.code, details, uncertain=False)
        self._record(request_id, "mimo_request_completed", {
            **details, "usage": result["usage"], "actual_model_calls": self.actual_model_calls,
            "usage_settled": True, "attempt_is_not_billing_confirmation": True,
        })
        return result
