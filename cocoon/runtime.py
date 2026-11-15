"""纯标准库运行时；事件存储仅供单进程使用，不提供跨进程安全保证。"""

import copy
import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


_SENSITIVE_KEYS = frozenset(
    {"api_key", "authorization", "access_token", "password", "secret", "refresh_token",
     "id_token", "client_secret", "private_key", "api_secret", "cookie", "set_cookie",
     "proxy_authorization", "x_api_key"}
)
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_METADATA = frozenset(
    {
        "schema_version", "experiment_id", "event_id", "logical_seq",
        "wall_time_utc", "config_hash", "material_manifest_hash", "is_demo",
        "previous_event_hash", "event_hash", "event_type",
    }
)
_STABLE_METADATA = (
    "experiment_id", "config_hash", "material_manifest_hash", "is_demo"
)
_AUDIT_NOTICE = (
    "这里不验证组织段是否含新增事实，须进入人工/独立引用审计；"
    "这不是正式 I4 原产品集成。"
)


def _json_text(obj):
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )


def canonical_hash(obj) -> str:
    """对排序、紧凑、非 ASCII 转义且不允许 NaN 的 JSON 求 SHA-256。"""
    return hashlib.sha256(_json_text(obj).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_sensitive_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.casefold().replace("-", "_") in _SENSITIVE_KEYS:
                raise ValueError("不允许写入敏感字段键")
            _reject_sensitive_keys(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _reject_sensitive_keys(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 含重复字段键")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("JSON 不允许非有限数值")


def _parse_json(data):
    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return json.loads(
            data, object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise ValueError("JSON 内容无效") from exc


def write_json_new(path: Path, obj):
    """父目录必须已存在；使用 x 模式创建并 fsync，绝不覆盖。"""
    _reject_sensitive_keys(obj)
    text = _json_text(obj)
    path = Path(path)
    if not path.parent.is_dir():
        raise FileNotFoundError("JSON 文件的父目录不存在")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _existing_root(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError("root 必须是已存在的目录")
    return root


def _storage_directory(root, relative, create=False):
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("存储目录不允许符号链接")
        if create:
            current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise ValueError("缺失存储目录")
        if not current.resolve().is_relative_to(root):
            raise ValueError("存储目录越出 root")
    return current


def _log_path(root):
    path = _storage_directory(root, "events") / "events.jsonl"
    if path.is_symlink() or not path.is_file():
        raise ValueError("事件日志缺失或不是普通文件")
    return path


def _blob_path(root, ref):
    if not isinstance(ref, str) or "\\" in ref:
        raise ValueError("blob 引用必须是相对 POSIX 路径")
    parts = PurePosixPath(ref).parts
    if len(parts) != 3 or parts[:2] != ("raw", "blobs"):
        raise ValueError("blob 引用路径无效")
    name = parts[2]
    if not name.endswith(".json") or not _HASH_RE.fullmatch(name[:-5]):
        raise ValueError("blob 文件名必须包含完整 SHA-256")
    if ref != f"raw/blobs/{name}":
        raise ValueError("blob 引用不允许路径规范化或穿越")
    path = _storage_directory(root, "raw/blobs") / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("blob 引用越界或使用符号链接")
    return path, name[:-5]


def _descriptor(obj):
    digest = canonical_hash(obj)
    return {"ref": f"raw/blobs/{digest}.json", "sha256": digest}


def _verify_blob(root, ref, expected_hash=None, pending=None):
    if isinstance(ref, dict):
        if "ref" not in ref or "sha256" not in ref:
            raise ValueError("引用对象必须含 ref 和 sha256")
        if expected_hash is not None and expected_hash != ref["sha256"]:
            raise ValueError("引用的 SHA-256 不一致")
        expected_hash = ref["sha256"]
        if not isinstance(expected_hash, str) or not _HASH_RE.fullmatch(expected_hash):
            raise ValueError("引用的 SHA-256 无效")
        ref = ref["ref"]
    path, digest = _blob_path(root, ref)
    if expected_hash is not None and expected_hash != digest:
        raise ValueError("引用 SHA-256 与文件名不一致")
    if path.exists():
        if not path.is_file():
            raise ValueError("blob 不是普通文件")
        data = path.read_bytes()
    elif pending is not None and ref in pending:
        data = pending[ref]
    else:
        raise ValueError("引用的 blob 缺失")
    obj = _parse_json(data)
    _reject_sensitive_keys(obj)
    if hashlib.sha256(data).hexdigest() != digest or canonical_hash(obj) != digest:
        raise ValueError("blob 内容或 SHA-256 被篡改")
    return obj


def _verify_references(root, value, pending=None):
    if isinstance(value, dict):
        if "ref" in value:
            _verify_blob(root, value, pending=pending)
        for key, item in value.items():
            if key.endswith("_ref"):
                expected = value.get(key[:-4] + "_sha256")
                if item is not None:
                    _verify_blob(root, item, expected, pending)
                elif expected is not None:
                    raise ValueError("空引用不能携带 SHA-256")
            elif key.endswith("_refs"):
                if item is not None:
                    if not isinstance(item, list):
                        raise ValueError("引用集合必须是列表")
                    for ref in item:
                        _verify_blob(root, ref, pending=pending)
            _verify_references(root, item, pending)
    elif isinstance(value, list):
        for item in value:
            _verify_references(root, item, pending)


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _validate_event(root, event, previous, seen_ids, pending=None):
    if not isinstance(event, dict) or not _METADATA.issubset(event):
        raise ValueError("事件缺少协议字段")
    _reject_sensitive_keys(event)
    if event["schema_version"] != "1.0":
        raise ValueError("不支持的事件协议版本")
    for key in ("experiment_id", "event_id", "event_type", "material_manifest_hash"):
        if not _nonempty_string(event[key]):
            raise ValueError("事件标识字段为空或类型不正确")
    if event["event_id"] in seen_ids:
        raise ValueError("event_id 重复")
    sequence = previous["logical_seq"] + 1 if previous else 1
    if type(event["logical_seq"]) is not int or event["logical_seq"] != sequence:
        raise ValueError("logical_seq 不连续")
    expected_previous = previous["event_hash"] if previous else None
    if event["previous_event_hash"] != expected_previous:
        raise ValueError("previous_event_hash 链断裂")
    if previous and any(event[key] != previous[key] for key in _STABLE_METADATA):
        raise ValueError("日志内的实验元数据不一致")
    if type(event["is_demo"]) is not bool:
        raise ValueError("is_demo 必须是布尔值")
    config_hash = event["config_hash"]
    if not isinstance(config_hash, str) or not _HASH_RE.fullmatch(config_hash):
        raise ValueError("config_hash 无效")
    wall_time = event["wall_time_utc"]
    if not isinstance(wall_time, str) or not wall_time.endswith("Z"):
        raise ValueError("wall_time_utc 必须是以 Z 结尾的 UTC 时间")
    try:
        parsed_time = datetime.fromisoformat(wall_time[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("wall_time_utc 无效") from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset().total_seconds() != 0:
        raise ValueError("wall_time_utc 不是 UTC 时间")
    parents = event.get("parent_event_ids")
    if parents is not None:
        if not isinstance(parents, list):
            raise ValueError("parent_event_ids 必须是列表")
        if any(not isinstance(parent, str) or parent not in seen_ids for parent in parents):
            raise ValueError("parent_event_ids 只能引用此前事件")
    for prefix in ("input", "output"):
        if event.get(prefix + "_sha256") is not None and event.get(prefix + "_ref") is None:
            raise ValueError("SHA-256 缺少对应引用")
    digest = event["event_hash"]
    unhashed = {key: value for key, value in event.items() if key != "event_hash"}
    if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
        raise ValueError("event_hash 无效")
    if canonical_hash(unhashed) != digest:
        raise ValueError("event_hash 校验失败")
    _verify_references(root, event, pending)
    if "payload" in event:
        _verify_blob(root, _descriptor(event["payload"]), pending=pending)


def _scan_store(root):
    events = []
    seen_ids = set()
    previous = None
    with _log_path(root).open("rb") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.endswith(b"\n") or not line.strip():
                raise ValueError(f"事件日志第 {line_number} 行断行或为空")
            try:
                event = _parse_json(line)
                _validate_event(root, event, previous, seen_ids)
            except (ValueError, OSError, TypeError) as exc:
                raise ValueError(f"事件日志第 {line_number} 行校验失败") from exc
            events.append(event)
            seen_ids.add(event["event_id"])
            previous = event
    blobs = _storage_directory(root, "raw/blobs")
    blob_count = 0
    for path in sorted(blobs.iterdir()):
        _verify_blob(root, path.relative_to(root).as_posix())
        blob_count += 1
    return events, blob_count


def verify_events(root: Path) -> dict:
    """完整验证日志、父事件关系及所有 blob；不存在日志时拒绝。"""
    events, blob_count = _scan_store(_existing_root(root))
    return {"event_count": len(events), "blob_count": blob_count, "chain_valid": True}


class EventStore:
    """仅用于单进程的追加式 JSONL 存储；不保证跨进程并发安全。"""

    def __init__(
        self, root: Path, config: dict, manifest_hash: str,
        is_demo: bool, experiment_id: str,
    ):
        self.root = _existing_root(root)
        if not isinstance(config, dict):
            raise TypeError("config 必须是字典")
        _reject_sensitive_keys(config)
        self.config_hash = canonical_hash(config)
        if not _nonempty_string(manifest_hash) or not _nonempty_string(experiment_id):
            raise ValueError("manifest_hash 和 experiment_id 不能为空")
        if type(is_demo) is not bool:
            raise TypeError("is_demo 必须是布尔值")
        self.manifest_hash = manifest_hash
        self.is_demo = is_demo
        self.experiment_id = experiment_id
        _storage_directory(self.root, "events", create=True)
        _storage_directory(self.root, "raw/blobs", create=True)
        path = self.root / "events/events.jsonl"
        if path.is_symlink():
            raise ValueError("事件日志不允许符号链接")
        if not path.exists():
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.flush()
                os.fsync(stream.fileno())
        events, _ = _scan_store(self.root)
        if events:
            expected = {
                "experiment_id": experiment_id,
                "config_hash": self.config_hash,
                "material_manifest_hash": manifest_hash,
                "is_demo": is_demo,
            }
            if any(events[-1][key] != value for key, value in expected.items()):
                raise ValueError("已有日志不属于相同实验及配置")
        self._last_event = events[-1] if events else None
        self._seen_ids = {event["event_id"] for event in events}
        self._log_size = path.stat().st_size

    def capture(self, obj) -> dict:
        _reject_sensitive_keys(obj)
        descriptor = _descriptor(obj)
        path, _ = _blob_path(self.root, descriptor["ref"])
        if path.exists():
            _verify_blob(self.root, descriptor)
        else:
            write_json_new(path, obj)
        return descriptor

    def append(self, event_type: str, **fields) -> dict:
        """保留所有调用方字段；协议保留字段冲突、敏感键或坏引用均拒绝。"""
        if not _nonempty_string(event_type):
            raise ValueError("event_type 不能为空")
        if _METADATA.intersection(fields):
            raise ValueError("调用方不能覆盖事件协议字段")
        _reject_sensitive_keys(fields)
        event = _parse_json(_json_text(fields))
        path = _log_path(self.root)
        if path.stat().st_size != self._log_size:
            raise ValueError("日志已被其他写入修改，请重新加载并校验")
        pending = {}
        if "payload" in event:
            payload_ref = _descriptor(event["payload"])
            pending[payload_ref["ref"]] = _json_text(event["payload"]).encode("utf-8")
            if "output_ref" not in event:
                event["output_ref"] = payload_ref["ref"]
                event.setdefault("output_sha256", payload_ref["sha256"])
        event.update(
            schema_version="1.0",
            experiment_id=self.experiment_id,
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            logical_seq=self._last_event["logical_seq"] + 1 if self._last_event else 1,
            wall_time_utc=utc_now(),
            config_hash=self.config_hash,
            material_manifest_hash=self.manifest_hash,
            is_demo=self.is_demo,
            previous_event_hash=self._last_event["event_hash"] if self._last_event else None,
        )
        event["event_hash"] = canonical_hash(event)
        _validate_event(self.root, event, self._last_event, self._seen_ids, pending)
        if "payload" in event:
            self.capture(event["payload"])
        line = _json_text(event) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        self._log_size += len(line.encode("utf-8"))
        self._last_event = event
        self._seen_ids.add(event["event_id"])
        return copy.deepcopy(event)


def save_snapshot(path: Path, state: dict) -> dict:
    """不覆盖地保存 envelope；返回的 sha256 是 state 的规范 JSON 哈希。"""
    if not isinstance(state, dict):
        raise TypeError("state 必须是字典")
    _reject_sensitive_keys(state)
    state_hash = canonical_hash(state)
    write_json_new(path, {"state": state, "state_sha256": state_hash})
    return {"ref": str(Path(path).resolve()), "sha256": state_hash}


def load_snapshot(path: Path) -> dict:
    envelope = _parse_json(Path(path).read_bytes())
    _reject_sensitive_keys(envelope)
    if not isinstance(envelope, dict) or set(envelope) != {"state", "state_sha256"}:
        raise ValueError("快照 envelope 格式无效")
    state = envelope["state"]
    if not isinstance(state, dict) or canonical_hash(state) != envelope["state_sha256"]:
        raise ValueError("快照 state_sha256 校验失败")
    return copy.deepcopy(state)


def branch_state(state: dict, world_id: str) -> dict:
    branch = copy.deepcopy(state)
    branch["parent_world_id"] = branch["world_id"]
    branch["world_id"] = world_id
    return branch


def measurement_view(state) -> dict:
    """测量方可以修改返回副本，但不会修改原状态或写入任何文件。"""
    return copy.deepcopy(state)


def make_intervention_pair(
    packet: dict, i4_text: str, i5_text: str,
    min_chars=600, max_chars=800, tolerance=.1,
) -> dict:
    """保留证据及组织段原文；不验证新增事实，必须人工/独立引用审计。

    非正式 I4 原产品集成。字符数是完整 visible_text 的 Python len；
    相对长度差按 abs(I4-I5)/min(I4,I5) 计算，超限仅记录偏差。
    """
    if not isinstance(packet, dict) or set(packet) != {"packet_id", "evidence"}:
        raise ValueError("packet 仅允许 packet_id 和 evidence")
    if not _nonempty_string(packet["packet_id"]):
        raise ValueError("packet_id 不能为空")
    evidence = packet["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("evidence 必须是非空列表")
    allowed = {"evidence_id", "text", "source_url", "source_family_id"}
    ids = []
    blocks = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != allowed:
            raise ValueError("证据字段不符合白名单，禁止附带标签或分析字段")
        if any(not _nonempty_string(item[key]) for key in allowed):
            raise ValueError("证据字段不能为空")
        evidence_id = item["evidence_id"]
        if evidence_id in ids:
            raise ValueError("evidence_id 重复")
        ids.append(evidence_id)
        blocks.append(
            f"[证据 {evidence_id}]\n{item['text']}\n"
            f"来源：{item['source_url']}\n来源族：{item['source_family_id']}"
        )
    if not _nonempty_string(i4_text) or not _nonempty_string(i5_text):
        raise ValueError("组织段不能为空")
    if type(min_chars) is not int or type(max_chars) is not int:
        raise TypeError("字符数边界必须是整数")
    if min_chars < 0 or max_chars < min_chars:
        raise ValueError("字符数边界无效")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        raise TypeError("tolerance 必须是有限数值")
    if not math.isfinite(tolerance) or not 0 <= tolerance <= 1:
        raise ValueError("tolerance 必须在 0 到 1 之间")
    block = "\n\n".join(blocks)
    evidence_hash = canonical_hash(block)
    packet_hash = canonical_hash(packet)
    deliveries = {}
    deviations = []
    for arm, additional_text in (("I4", i4_text), ("I5", i5_text)):
        visible_text = block + "\n\n" + additional_text
        char_count = len(visible_text)
        deliveries[arm] = {
            "packet_id": packet["packet_id"],
            "evidence_ids": list(ids),
            "evidence_block": block,
            "evidence_hash": evidence_hash,
            "visible_text": visible_text,
            "char_count": char_count,
            "packet_hash": packet_hash,
            "additional_text": additional_text,
        }
        if not min_chars <= char_count <= max_chars:
            deviations.append({
                "code": "dose_out_of_range", "arm": arm,
                "char_count": char_count, "min_chars": min_chars, "max_chars": max_chars,
            })
    left, right = deliveries["I4"], deliveries["I5"]
    difference = abs(left["char_count"] - right["char_count"])
    ratio = difference / min(left["char_count"], right["char_count"])
    length_matched = ratio <= tolerance
    if not length_matched:
        deviations.append({
            "code": "length_mismatch", "difference_ratio": ratio,
            "tolerance": tolerance,
        })
    deliveries["checks"] = {
        "evidence_equal": all(
            left[key] == right[key]
            for key in ("evidence_ids", "evidence_block", "evidence_hash", "packet_hash")
        ),
        "dose_within_range": all(
            min_chars <= delivery["char_count"] <= max_chars
            for delivery in (left, right)
        ),
        "length_matched": length_matched,
        "length_difference_ratio": ratio,
        "length_comparison_basis": "abs(I4-I5)/min(I4,I5)",
        "semantic_review": "pending",
        "audit_notice": _AUDIT_NOTICE,
        "deviations": deviations,
    }
    return deliveries
