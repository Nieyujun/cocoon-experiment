"""仅标准库的 MiMo flash 累计预算账本，不联网、不调用模型。

价格于 2026-11-08 经主控官方核验，官方价格网页更新日为 2026-10-21。
主控须在拿到密钥后、实际请求前成功 reserve，并实际传递同一个
max_completion_tokens。1048576 token 是保守输入上限，不是精确估计。
unknown 费用不等于免费；这里按供应商 usage 和公开费率记账，不是发票。
日志及其 .lock 文件必须保留，不能靠更换路径或删除日志重置累计预算。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, DecimalException, Inexact, localcontext
from hashlib import sha256
import errno
import json
import os
import re
from pathlib import Path
import threading

if os.name == "nt":
    import msvcrt
else:
    import fcntl


RATE_INPUT_CNY_PER_MILLION = Decimal("1.00")
RATE_CACHED_INPUT_CNY_PER_MILLION = Decimal("0.02")
RATE_OUTPUT_CNY_PER_MILLION = Decimal("2.00")
PRICE_SOURCE_URL = "https://mimo.mi.com/docs/zh-CN/price/pay-as-you-go"
PRICE_VERIFIED_DATE = "2026-11-08"
PRICE_EFFECTIVE_DATE = "2026-10-21"
INPUT_RESERVE_TOKENS = 1048576
MAX_COMPLETION_TOKENS = 131072
COST_BASIS = "provider_usage_official_rate_not_invoice"
UNCERTAIN_REASONS = frozenset({
    "http_401", "http_error", "timeout", "network_error", "connection_error",
    "invalid_response", "missing_usage", "invalid_usage", "interrupted",
})

__all__ = [
    "BudgetError", "BudgetLedger", "RATE_INPUT_CNY_PER_MILLION",
    "RATE_CACHED_INPUT_CNY_PER_MILLION", "RATE_OUTPUT_CNY_PER_MILLION",
    "PRICE_SOURCE_URL", "PRICE_VERIFIED_DATE", "PRICE_EFFECTIVE_DATE", "INPUT_RESERVE_TOKENS",
    "MAX_COMPLETION_TOKENS", "COST_BASIS", "UNCERTAIN_REASONS",
]

_MILLION = Decimal("1000000")
_ZERO = Decimal("0")
_QUANTUM = Decimal("0.00000001")
_CONTEXT = Context(prec=50)
_CONTEXT.traps[Inexact] = True
_GENESIS = "0" * 64
_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_ANOMALY_REASONS = frozenset({
    "input_bound_exceeded", "completion_limit_exceeded", "web_search_usage",
    "reservation_amount_exceeded",
})
_META_FIELDS = {"seq", "type", "timestamp", "prev_hash", "hash"}
_EVENT_FIELDS = {
    "init": {"version", "limit_cny", "policy"},
    "reserve": {"request_id", "max_completion_tokens", "reserved_cny"},
    "settle": {"request_id", "usage", "charged_cny"},
    "uncertain": {"request_id", "reason"},
    "blocked": {"request_id", "reason"},
    # 受控提额：只升不降，previous 必须等于回放时的有效上限；不涉及任何请求。
    "raise_limit": {"previous_limit_cny", "new_limit_cny", "reason"},
}
_RAISE_REASON = re.compile(r"^[a-z0-9_]{1,120}$")


class BudgetError(ValueError):
    """预算拒绝的固定错误码；不包含上游响应正文。"""

    def __init__(self, code: str):
        self._code = code
        super().__init__(code)

    @property
    def code(self) -> str:
        return self._code


def _money(value: Decimal) -> str:
    whole, _, fraction = format(value, "f").partition(".")
    return whole + "." + fraction.rstrip("0").ljust(2, "0")


def _limit(value) -> Decimal:
    # 拒绝 float；固定八位最小货币单位覆盖缓存单 token 的公开费用。
    if type(value) not in (str, int, Decimal):
        raise BudgetError("invalid_limit")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0:
            raise BudgetError("invalid_limit")
        return amount.quantize(_QUANTUM, context=_CONTEXT.copy())
    except (DecimalException, ValueError, OverflowError):
        raise BudgetError("invalid_limit") from None


def _request_id(value) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BudgetError("invalid_request_id")


def _output_limit(value) -> None:
    if type(value) is not int or not 1 <= value <= MAX_COMPLETION_TOKENS:
        raise BudgetError("invalid_max_completion_tokens")


def _reservation_amount(max_completion_tokens: int) -> Decimal:
    return (
        Decimal(INPUT_RESERVE_TOKENS) * RATE_INPUT_CNY_PER_MILLION
        + Decimal(max_completion_tokens) * RATE_OUTPUT_CNY_PER_MILLION
    ) / _MILLION


def _normalise_usage(usage: dict) -> dict:
    if not isinstance(usage, dict):
        raise BudgetError("invalid_usage")
    counts = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(name)
        if type(value) is not int or value < 0:
            raise BudgetError("invalid_usage")
        counts[name] = value
    if counts["total_tokens"] != counts["prompt_tokens"] + counts["completion_tokens"]:
        raise BudgetError("invalid_usage")
    details = usage.get("prompt_tokens_details")
    if details is not None and not isinstance(details, dict):
        raise BudgetError("invalid_usage")
    cached = details.get("cached_tokens") if details is not None else None
    if cached is not None and (
        type(cached) is not int or not 0 <= cached <= counts["prompt_tokens"]
    ):
        raise BudgetError("invalid_usage")
    search = usage.get("web_search_usage")
    # 缺省/null 视为未使用搜索；兼容旧整数输入，落盘统一为双字段对象。
    if search is None:
        search = {"tool_usage": 0, "page_usage": 0}
    elif type(search) is int:
        search = {"tool_usage": search, "page_usage": 0}
    if not isinstance(search, dict):
        raise BudgetError("invalid_usage")
    search_counts = {}
    for name in ("tool_usage", "page_usage"):
        value = search.get(name)
        if type(value) is not int or value < 0:
            raise BudgetError("invalid_usage")
        search_counts[name] = value
    # 只持久化计费字段；不保留供应商的正文、错误消息或未知元数据。
    return counts | {
        "prompt_tokens_details": None if cached is None else {"cached_tokens": cached},
        "web_search_usage": search_counts,
    }


def _charge(usage: dict) -> Decimal:
    details = usage["prompt_tokens_details"]
    hits = 0 if details is None else details["cached_tokens"]
    return (
        Decimal(hits) * RATE_CACHED_INPUT_CNY_PER_MILLION
        + Decimal(usage["prompt_tokens"] - hits) * RATE_INPUT_CNY_PER_MILLION
        + Decimal(usage["completion_tokens"]) * RATE_OUTPUT_CNY_PER_MILLION
    ) / _MILLION


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 字段")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("非法 JSON 数字")


def _lock_file(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        # Windows 允许锁住 EOF 之后的字节，空锁文件无需先写入或截断。
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@dataclass
class _Reservation:
    request_id: str
    max_completion_tokens: int
    amount: Decimal
    state: str = "reserved"
    charged: Decimal | None = None
    reason: str | None = None
    cache_assumed_zero: bool = False


# 用户逐条授权的“中断持有”预留（步骤40，2026-11-13）：主控进程在 HTTP 在途时被外部终止，
# 用量未知。该预留已在账本登记 uncertain/interrupted，按最坏情况金额永久计入 reserved（仍占额度、
# 仍计 unresolved/needs_reconciliation），但不再阻止后续新预留。只放行此处显式列出的请求 ID，
# 且必须同时处于 uncertain+interrupted 状态；其它任何未决原因照旧串行阻塞。拿到真实用量后仍可 settle。
HELD_INTERRUPTED_REQUEST_IDS = frozenset({
    "live_micro_pilot_TOPIC_01_20261113T064439Z_91886789:respond_S01_I4_R1:1",
})


# 步骤42（2026-11-14）用户逐条授权：封闭情境第三次运行一条盲评请求收到上游 HTTP 500（响应 102 字节），
# 账本已登记 uncertain/http_error。规则与上同：按最坏情况金额持有、仍计未决与需对账、不伪造结算，
# 只是不再阻止后续新预留；仅限此 ID 且必须处于 uncertain+http_error 状态，拿到真实用量后仍可 settle。
HELD_HTTP_ERROR_REQUEST_IDS = frozenset({
    "closed_world_synth01_20261114T043249Z_fa670af9:blind_BLIND_0005:1",
})


def _held_interrupted(reservation) -> bool:
    if reservation.state != "uncertain":
        return False
    return ((reservation.request_id in HELD_INTERRUPTED_REQUEST_IDS and reservation.reason == "interrupted")
            or (reservation.request_id in HELD_HTTP_ERROR_REQUEST_IDS and reservation.reason == "http_error"))


@dataclass
class _State:
    seq: int = 0
    head: str = _GENESIS
    requests: dict = field(default_factory=dict)
    charged: Decimal = _ZERO
    reserved: Decimal = _ZERO
    blocked: bool = False
    limit: Decimal | None = None
    raises: int = 0


class BudgetLedger:
    """一个日志路径代表一份跨实例、跨进程、跨运行的累计预算。

    空文件追加 init 记录，已有文件只校验、不重置。金额接受字符串、整数或
    Decimal，必须为正、能精确表示到小数点后八位且不溢出 50 位 Decimal。
    首次按量测试刻意串行：任何未决预留均拒绝新的尝试，包括进程重建后。
    同 request_id 永不重复预留，无 cancel 接口。损坏日志不会自动修复。
    """

    def __init__(self, path: Path, limit_cny="5.00"):
        self._limit = _limit(limit_cny)
        if not isinstance(path, Path) or not path.parent.is_dir():
            raise BudgetError("invalid_path")
        self._path = path.resolve()
        self._lock_path = Path(str(self._path) + ".lock")
        key = os.path.normcase(str(self._path))
        if not _THREAD_LOCKS_GUARD.acquire(blocking=False):
            raise BudgetError("budget_busy")
        try:
            self._thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
        finally:
            _THREAD_LOCKS_GUARD.release()
        with self._transaction(initialize=True):
            pass

    def _policy(self) -> dict:
        return {
            "rate_input_cny_per_million": _money(RATE_INPUT_CNY_PER_MILLION),
            "rate_cached_input_cny_per_million": _money(RATE_CACHED_INPUT_CNY_PER_MILLION),
            "rate_output_cny_per_million": _money(RATE_OUTPUT_CNY_PER_MILLION),
            "input_reserve_tokens": INPUT_RESERVE_TOKENS,
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
            "price_source_url": PRICE_SOURCE_URL,
            "price_verified_date": PRICE_VERIFIED_DATE,
            "price_effective_date": PRICE_EFFECTIVE_DATE,
            "cost_basis": COST_BASIS,
        }

    @contextmanager
    def _locked(self):
        if not self._thread_lock.acquire(blocking=False):
            raise BudgetError("budget_busy")
        try:
            with self._lock_path.open("a+b") as stream:
                try:
                    _lock_file(stream)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(
                        exc, "winerror", None
                    ) in (33, 36):
                        raise BudgetError("budget_busy") from None
                    raise
                try:
                    yield
                finally:
                    _unlock_file(stream)
        except OSError:
            raise BudgetError("budget_io_error") from None
        finally:
            self._thread_lock.release()

    @contextmanager
    def _transaction(self, initialize=False):
        with self._locked(), localcontext(_CONTEXT):
            flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_BINARY", 0)
            if initialize:
                flags |= os.O_CREAT
            try:
                descriptor = os.open(self._path, flags, 0o600)
                with os.fdopen(descriptor, "a+b") as stream:
                    state = self._replay(stream)
                    if state.seq == 0:
                        if not initialize:
                            raise BudgetError("ledger_corrupt")
                        self._append(stream, state, "init", {
                            "version": 1, "limit_cny": _money(self._limit),
                            "policy": self._policy(),
                        })
                    yield stream, state
            except OSError:
                raise BudgetError("budget_io_error") from None

    def _replay(self, stream) -> _State:
        state = _State()
        stream.seek(0)
        for line in stream:
            try:
                if not line.endswith(b"\n") or not line.strip():
                    raise ValueError("日志有部分行或空行")
                record = json.loads(
                    line, object_pairs_hook=_unique_object, parse_constant=_reject_constant
                )
                self._consume(state, record)
            except BudgetError as exc:
                if exc.code == "limit_mismatch":
                    raise
                raise BudgetError("ledger_corrupt") from None
            except (ValueError, TypeError, KeyError, DecimalException, RecursionError):
                raise BudgetError("ledger_corrupt") from None
        return state

    def _consume(self, state: _State, record: dict) -> None:
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            raise BudgetError("ledger_corrupt")
        kind = record["type"]
        fields = _EVENT_FIELDS.get(kind)
        if fields is None or set(record) != _META_FIELDS | fields:
            raise BudgetError("ledger_corrupt")
        if type(record["seq"]) is not int or record["seq"] != state.seq + 1:
            raise BudgetError("ledger_corrupt")
        if record["prev_hash"] != state.head:
            raise BudgetError("ledger_corrupt")
        if sha256(_canonical({k: v for k, v in record.items() if k != "hash"})).hexdigest() != record["hash"]:
            raise BudgetError("ledger_corrupt")
        stamp = datetime.fromisoformat(record["timestamp"])
        if stamp.utcoffset() != timedelta(0):
            raise BudgetError("ledger_corrupt")
        if kind == "init":
            if state.seq or type(record["version"]) is not int or record["version"] != 1:
                raise BudgetError("ledger_corrupt")
            if _canonical(record["policy"]) != _canonical(self._policy()):
                raise BudgetError("ledger_corrupt")
            if type(record["limit_cny"]) is not str:
                raise BudgetError("ledger_corrupt")
            persisted_limit = _limit(record["limit_cny"])
            if record["limit_cny"] != _money(persisted_limit):
                raise BudgetError("ledger_corrupt")
            if persisted_limit != self._limit:
                raise BudgetError("limit_mismatch")
            state.limit = persisted_limit
        elif kind == "raise_limit":
            if state.seq == 0 or state.limit is None:
                raise BudgetError("ledger_corrupt")
            if state.blocked:
                raise BudgetError("budget_blocked")
            for key in ("previous_limit_cny", "new_limit_cny", "reason"):
                if type(record[key]) is not str:
                    raise BudgetError("ledger_corrupt")
            if record["previous_limit_cny"] != _money(state.limit):
                raise BudgetError("ledger_corrupt")
            new_limit = _limit(record["new_limit_cny"])
            if record["new_limit_cny"] != _money(new_limit):
                raise BudgetError("ledger_corrupt")
            if new_limit <= state.limit:
                raise BudgetError("limit_not_increased")
            if not _RAISE_REASON.fullmatch(record["reason"]):
                raise BudgetError("invalid_reason")
            state.limit = new_limit
            state.raises += 1
        else:
            if state.seq == 0:
                raise BudgetError("ledger_corrupt")
            request_id = record["request_id"]
            _request_id(request_id)
            if kind == "reserve":
                _output_limit(record["max_completion_tokens"])
                if request_id in state.requests:
                    raise BudgetError("duplicate_request")
                if state.blocked:
                    raise BudgetError("budget_blocked")
                if any(r.charged is None and not _held_interrupted(r)
                       for r in state.requests.values()):
                    raise BudgetError("unresolved_attempt")
                amount = _reservation_amount(record["max_completion_tokens"])
                if record["reserved_cny"] != _money(amount):
                    raise BudgetError("ledger_corrupt")
                if amount > state.limit - state.charged - state.reserved:
                    raise BudgetError("budget_exceeded")
                state.requests[request_id] = _Reservation(
                    request_id, record["max_completion_tokens"], amount
                )
                state.reserved += amount
            else:
                reservation = self._unresolved(state, request_id)
                if kind == "settle":
                    usage = _normalise_usage(record["usage"])
                    if _canonical(usage) != _canonical(record["usage"]):
                        raise BudgetError("ledger_corrupt")
                    if self._anomaly(reservation, usage):
                        raise BudgetError("billing_anomaly")
                    charged = _charge(usage)
                    if record["charged_cny"] != _money(charged):
                        raise BudgetError("ledger_corrupt")
                    reservation.state = "settled"
                    reservation.charged = charged
                    reservation.cache_assumed_zero = usage["prompt_tokens_details"] is None
                    state.charged += charged
                    state.reserved -= reservation.amount
                else:
                    allowed = UNCERTAIN_REASONS if kind == "uncertain" else _ANOMALY_REASONS
                    if not isinstance(record["reason"], str) or record["reason"] not in allowed:
                        raise BudgetError("invalid_reason")
                    reservation.state = kind
                    reservation.reason = record["reason"]
                    if kind == "blocked":
                        state.blocked = True
        state.seq = record["seq"]
        state.head = record["hash"]

    def _append(self, stream, state: _State, kind: str, fields: dict) -> None:
        record = {
            "seq": state.seq + 1, "type": kind,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prev_hash": state.head,
        } | fields
        record["hash"] = sha256(_canonical(record)).hexdigest()
        self._consume(state, record)
        payload = _canonical(record) + b"\n"
        stream.seek(0, os.SEEK_END)
        if stream.write(payload) != len(payload):
            raise BudgetError("budget_io_error")
        stream.flush()
        os.fsync(stream.fileno())

    def _unresolved(self, state: _State, request_id: str) -> _Reservation:
        _request_id(request_id)
        reservation = state.requests.get(request_id)
        if reservation is None:
            raise BudgetError("unknown_request")
        if reservation.state == "settled":
            raise BudgetError("already_settled")
        if reservation.state == "blocked":
            raise BudgetError("billing_anomaly")
        return reservation

    def _anomaly(self, reservation: _Reservation, usage: dict) -> str | None:
        if usage["prompt_tokens"] > INPUT_RESERVE_TOKENS:
            return "input_bound_exceeded"
        if usage["completion_tokens"] > reservation.max_completion_tokens:
            return "completion_limit_exceeded"
        if any(value > 0 for value in usage["web_search_usage"].values()):
            return "web_search_usage"
        if _charge(usage) > reservation.amount:
            return "reservation_amount_exceeded"
        return None

    def _request_status(self, reservation: _Reservation) -> dict:
        return {
            "request_id": reservation.request_id,
            "reservation_id": reservation.request_id,
            "max_completion_tokens": reservation.max_completion_tokens,
            "state": reservation.state,
            "reserved_cny": _money(reservation.amount if reservation.charged is None else _ZERO),
            "charged_cny": None if reservation.charged is None else _money(reservation.charged),
            "reason": reservation.reason,
            "cost_basis": COST_BASIS,
        }

    def status(self) -> dict:
        """charged 仅统计 settled；未决费用为 null，needs_reconciliation 提示核对。"""
        with self._transaction() as (_, state):
            unresolved = sum(r.charged is None for r in state.requests.values())
            return {
                "limit_cny": _money(state.limit),
                "initial_limit_cny": _money(self._limit),
                "limit_raise_count": state.raises,
                "charged_cny": _money(state.charged),
                "reserved_cny": _money(state.reserved),
                "available_cny": _money(state.limit - state.charged - state.reserved),
                "request_count": len(state.requests),
                "blocked": state.blocked,
                "needs_reconciliation": bool(unresolved),
                "cost_basis": COST_BASIS,
                "charged_scope": "settled_requests_only",
                "unresolved_request_count": unresolved,
                "uncertain_request_count": sum(r.state == "uncertain" for r in state.requests.values()),
                "held_interrupted_request_count": sum(
                    r.charged is None and _held_interrupted(r) for r in state.requests.values()),
                "unresolved_cost_cny": None if unresolved else "0.00",
                "total_cost_cny": None if unresolved else _money(state.charged),
                "cache_policy": "missing_or_null_cached_tokens_assumed_zero",
                "cache_assumed_zero_request_count": sum(r.cache_assumed_zero for r in state.requests.values()),
                "reservation_basis": "full_context_input_conservative_bound_not_estimate",
            }

    def reserve(self, request_id: str, max_completion_tokens: int) -> dict:
        """成功返回前追加并 fsync；调用方只能在成功后发起实际请求。"""
        _request_id(request_id)
        _output_limit(max_completion_tokens)
        with self._transaction() as (stream, state):
            self._append(stream, state, "reserve", {
                "request_id": request_id, "max_completion_tokens": max_completion_tokens,
                "reserved_cny": _money(_reservation_amount(max_completion_tokens)),
            })
            return self._request_status(state.requests[request_id])

    def settle(self, request_id, usage: dict) -> dict:
        """无效 usage 不写入也不释放；异常预留永久保留，封禁只能人工处理。"""
        with self._transaction() as (stream, state):
            reservation = self._unresolved(state, request_id)
            normalised = _normalise_usage(usage)
            anomaly = self._anomaly(reservation, normalised)
            if anomaly is not None:
                self._append(stream, state, "blocked", {"request_id": request_id, "reason": anomaly})
                raise BudgetError("billing_anomaly")
            self._append(stream, state, "settle", {
                "request_id": request_id, "usage": normalised,
                "charged_cny": _money(_charge(normalised)),
            })
            return self._request_status(state.requests[request_id])

    def raise_limit(self, new_limit_cny, reason: str) -> dict:
        """受控提额：追加 raise_limit 事件（哈希链 + fsync），新上限必须严格大于当前有效上限。

        不改写 init、不删除任何记录；封禁状态下拒绝提额；原因为小写短码。"""
        if not isinstance(reason, str) or not _RAISE_REASON.fullmatch(reason):
            raise BudgetError("invalid_reason")
        new_limit = _limit(new_limit_cny)
        with self._transaction() as (stream, state):
            if new_limit <= state.limit:
                raise BudgetError("limit_not_increased")
            if state.blocked:
                raise BudgetError("budget_blocked")
            self._append(stream, state, "raise_limit", {
                "previous_limit_cny": _money(state.limit),
                "new_limit_cny": _money(new_limit), "reason": reason,
            })
        return self.status()

    def mark_uncertain(self, request_id, reason: str) -> dict:
        """只接受 UNCERTAIN_REASONS 的短码；不发送、重试或取消上游请求。"""
        if not isinstance(reason, str) or reason not in UNCERTAIN_REASONS:
            raise BudgetError("invalid_reason")
        with self._transaction() as (stream, state):
            reservation = self._unresolved(state, request_id)
            if reservation.state != "uncertain" or reservation.reason != reason:
                self._append(stream, state, "uncertain", {"request_id": request_id, "reason": reason})
            return self._request_status(reservation)
