"""离线网络级分析层；不调用模型、不写文件、不生成正式效应结论。

个体×时点先聚合为网络均值，不把Agent或题目当独立网络；缺测固定分母保留。
仅调用现有metrics算子与scheduling.allocate，不修改这些模块。自含哈希不是
外部签名；真实allocation摘要及事件真实性仍须由控制器可信记录提供。
"""
from copy import deepcopy
from functools import wraps
import math
import random
import re

from . import metrics, runtime, scheduling

__all__ = ["aggregate_panel_to_networks", "analyze_exposure_2x2", "analyze_intervention_effects"]
ANALYSIS_VERSION = "analysis_contract_v1"
NETWORK_AGGREGATION_VERSION = "network_aggregation_v1"
_PANEL_VERSION = "reconciled_blind_panel_v1"
_POINTS = frozenset(("T0", "T1", "T2", "T3"))
_ARMS = ("I0", "I1", "I2", "I3", "I4", "I5")
_ENVIRONMENTS = ("A", "B", "C", "D")
_METRICS = {
    "BS": {"scale": 1.0, "higher_is_better": False, "min": 0.0, "max": 1.0},
    "Acc": {"scale": 100.0, "higher_is_better": True, "min": 0.0, "max": 100.0},
    "S": {"scale": 100.0, "higher_is_better": False, "min": 0.0, "max": 100.0},
    "O": {"scale": 100.0, "higher_is_better": False, "min": 0.0, "max": 100.0},
    "U": {"scale": 100.0, "higher_is_better": True, "min": 0.0, "max": 100.0},
}
_OBJECTIVE_METRICS = ("BS", "Acc", "S")
_SUBJECTIVE_METRICS = ("O", "U")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")

_PANEL_REQUIRED_FIELDS = (
    "contract", "panel_records", "panel_sha256", "is_demo", "formal_eligible", "new_model_calls",
)
_PANEL_ROW_REQUIRED_FIELDS = (
    "blind_sample_id", "world_id", "agent_id", "environment", "arm", "timepoint",
    "block_id", "topic_id", "is_demo", "BS", "Acc", "S", "BS_lower", "BS_upper",
    "O", "U", "blind_evaluation_status", "objective_complete", "row_complete",
)
_NETWORK_REQUIRED_FIELDS = (
    "contract", "expected_agents_per_world", "network_records", "network_records_sha256",
    "is_demo", "formal_eligible",
)
_NETWORK_ROW_REQUIRED_FIELDS = (
    "topic_id", "block_id", "world_id", "environment", "arm", "timepoint",
    "expected_agents", "observed_agents", "missing_agents", "objective_complete_count",
    "blind_complete_count", "valid", "blind_valid", "BS", "Acc", "S", "O", "U",
    "BS_lower_mean", "BS_upper_mean",
)


def _need(condition, code):
    if not condition:
        raise ValueError("analysis:" + code)


def _api(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ValueError as exc:
            if str(exc).startswith("analysis:"):
                raise
            raise ValueError("analysis:invalid_contract") from None
        except (TypeError, KeyError, IndexError, OverflowError, RecursionError):
            raise ValueError("analysis:invalid_contract") from None
    return call


class _ControlledDict(dict):
    """演示受控调用标记；metrics仍是原函数，输入语义不变。"""


def _fields(value, required, code, optional=()):
    _need(type(value) is dict and all(type(k) is str for k in value)
          and set(required) <= set(value) <= set(required) | set(optional), code)
    runtime._reject_sensitive_keys(value)
    runtime.canonical_hash(value)


def _loose_fields(value, required, code):
    """allocation允许附带只读审计元数据；必需字段及JSON安全性仍严格检查。"""
    _need(type(value) is dict and all(type(k) is str for k in value)
          and set(required) <= set(value), code)
    runtime._reject_sensitive_keys(value)
    runtime.canonical_hash(value)


def _hash(value):
    return runtime.canonical_hash(value)


def _text(value, code):
    _need(type(value) is str and bool(value.strip()) and len(value) <= 128, code)
    _need(any(ord(c) < 32 for c in value) is False, code)
    return value


def _identifier(value, code, allow_integer=False):
    if allow_integer and type(value) is int and value >= 0:
        return value
    _need(type(value) is str and _ID.fullmatch(value) is not None, code)
    return value


def _finite(value, code):
    _need(type(value) in (int, float) and math.isfinite(float(value)), code)
    return float(value)


def _unit(value, code):
    number = _finite(value, code)
    _need(0 <= number <= 1, code)
    return number


def _optional_finite(value):
    return None if value is None else _finite(value, "invalid_metric_value")


def _mean(values, denominator):
    _need(denominator > 0, "invalid_denominator")
    return math.fsum(values) / denominator


def _sort_key(value):
    return tuple(str(part) for part in value)


def _population_size(value):
    _need(type(value) is int, "expected_agents_invalid")
    try:
        scheduling._check_population_size(value)
    except (TypeError, ValueError):
        raise ValueError("analysis:expected_agents_invalid") from None


def _demo_flags(allow_demo, source_is_demo):
    _need(type(allow_demo) is bool and type(source_is_demo) is bool, "allow_demo_invalid")
    if source_is_demo and not allow_demo:
        raise ValueError("analysis:demo_requires_authorization")
    return bool(allow_demo or source_is_demo)


def _target_topics(value, available=None):
    _need(type(value) is list and bool(value)
          and all(type(v) is str and v for v in value) and len(set(value)) == len(value),
          "target_topics_invalid")
    if available is not None:
        _need(set(value) <= set(available), "target_topic_unavailable")
    return list(value)


def _metric_name(value):
    _need(type(value) is str and value in _METRICS, "metric_invalid")
    return value


def _metric_loss(metric, value):
    info = _METRICS[metric]
    number = _finite(value, "invalid_metric_value")
    _need(info["min"] <= number <= info["max"], "metric_out_of_range")
    if info["higher_is_better"]:
        return (info["max"] - number) / info["scale"]
    return number / info["scale"]


def _metric_bound(value, metric):
    return _unit(value, "invalid_metric_bound") if metric == "BS" else _optional_finite(value)


def _panel_row(row, panel_demo):
    _loose_fields(row, _PANEL_ROW_REQUIRED_FIELDS, "panel_row_fields")
    _identifier(row["blind_sample_id"], "sample_id_invalid")
    for name in ("world_id", "agent_id", "topic_id", "questionnaire_id", "environment"):
        _identifier(row[name], "panel_identifier_invalid")
    _identifier(row["block_id"], "panel_identifier_invalid", allow_integer=True)
    _need(row["timepoint"] in _POINTS, "timepoint_invalid")
    _need(row["arm"] is None or row["arm"] in _ARMS, "arm_invalid")
    _need(type(row["is_demo"]) is bool and row["is_demo"] == panel_demo, "panel_demo_mismatch")
    _need(type(row["objective_complete"]) is bool and type(row["row_complete"]) is bool, "panel_flag_invalid")
    _need(row["blind_evaluation_status"] in ("pending", "validated"), "blind_status_invalid")
    _need(row.get("formal_eligible", False) is False
          and row.get("controller_private_only", True) is True
          and row.get("actual_observation_verified", False) is False, "panel_row_identity_invalid")
    _need(row.get("primary_endpoint_partition", "anchor_eight_only") == "anchor_eight_only"
          and row.get("transfer_scope", "exploratory_only") == "exploratory_only", "panel_partition_invalid")
    for name in ("BS", "Acc", "S", "O", "U", "BS_lower", "BS_upper"):
        value = row[name]
        if value is not None:
            _finite(value, "panel_metric_invalid")
    if row["BS"] is not None:
        _unit(row["BS"], "panel_bs_out_of_range")
    anchor = row.get("anchor")
    if anchor is not None:
        _need(type(anchor) is dict and anchor.get("complete") is row["objective_complete"]
              and anchor.get("expected_n", 8) == 8, "panel_anchor_invalid")
    if row["objective_complete"]:
        _need(row["BS"] is not None and row["Acc"] is not None and row["S"] is not None,
              "panel_objective_incomplete")
    else:
        _need(row["BS"] is None and row["Acc"] is None and row["S"] is None,
              "panel_objective_contradiction")
    if row["row_complete"]:
        _need(row["blind_evaluation_status"] == "validated" and row["O"] is not None
              and row["U"] is not None, "panel_subjective_incomplete")
    else:
        _need(row["O"] is None and row["U"] is None, "panel_subjective_contradiction")
    if row["BS_lower"] is not None and row["BS_upper"] is not None:
        _need(_unit(row["BS_lower"], "panel_lower_invalid") <= _unit(row["BS_upper"], "panel_upper_invalid"),
              "panel_bounds_reversed")
    transfer = row.get("transfer")
    if transfer is not None:
        _need(type(transfer) is dict and type(transfer.get("complete")) is bool
              and transfer.get("expected_n", 4) in (0, 4), "panel_transfer_invalid")
        if transfer["complete"]:
            _need(transfer.get("BS") is not None and transfer.get("Acc") is not None
                  and transfer.get("S") is not None, "panel_transfer_incomplete")
        else:
            _need(transfer.get("BS") is None, "panel_transfer_contradiction")


def _bounds_terms(row):
    lower = 0.0 if row["BS_lower"] is None else _unit(row["BS_lower"], "panel_lower_invalid")
    upper = 1.0 if row["BS_upper"] is None else _unit(row["BS_upper"], "panel_upper_invalid")
    _need(lower <= upper, "panel_bounds_reversed")
    return lower, upper


def _transfer_summary(rows, expected_agents):
    transfers = [row.get("transfer") for row in rows]
    available = all(item is not None and item.get("expected_n", 4) == 4 for item in transfers)
    complete_count = sum(bool(item and item.get("complete")) for item in transfers)
    valid = len(rows) == expected_agents and available and complete_count == expected_agents
    summary = {"scope": "exploratory_only", "available": available, "expected_n": 4 if available else None,
               "complete_count": complete_count, "valid": valid, "BS": None, "Acc": None, "S": None,
               "BS_lower_mean": None, "BS_upper_mean": None, "primary_endpoint_separate": True}
    lower_terms, upper_terms = [], []
    for item in transfers:
        if item is None:
            lower_terms.append(0.0)
            upper_terms.append(1.0)
            continue
        lo = 0.0 if item.get("BS_lower") is None else _unit(item["BS_lower"], "transfer_bound_invalid")
        hi = 1.0 if item.get("BS_upper") is None else _unit(item["BS_upper"], "transfer_bound_invalid")
        _need(lo <= hi, "transfer_bounds_reversed")
        lower_terms.append(lo)
        upper_terms.append(hi)
    missing = expected_agents - len(rows)
    summary["BS_lower_mean"] = _mean(lower_terms + [0.0] * missing, expected_agents)
    summary["BS_upper_mean"] = _mean(upper_terms + [1.0] * missing, expected_agents)
    if valid:
        for name in ("BS", "Acc", "S"):
            summary[name] = _mean([_finite(item[name], "transfer_metric_invalid") for item in transfers], expected_agents)
    return summary


@_api
def aggregate_panel_to_networks(reconciled_panel, *, expected_agents_per_world=16, allow_demo=False) -> dict:
    """把步骤31个体长面板严格聚合为网络×时点记录；不静默丢弃缺测个体。"""
    before = _hash(reconciled_panel)
    _loose_fields(reconciled_panel, _PANEL_REQUIRED_FIELDS, "panel_fields")
    _need(reconciled_panel["contract"] == _PANEL_VERSION, "panel_contract_invalid")
    _need(type(reconciled_panel["is_demo"]) is bool, "panel_demo_invalid")
    _need(reconciled_panel["formal_eligible"] is False and reconciled_panel["new_model_calls"] == 0
          and reconciled_panel.get("controller_private_only", True) is True
          and reconciled_panel.get("send_to_evaluator", False) is False
          and reconciled_panel.get("writes_to_memory", False) is False, "panel_identity_invalid")
    _population_size(expected_agents_per_world)
    rows = reconciled_panel["panel_records"]
    _need(type(rows) is list, "panel_record_count")
    derived_completed = sum(bool(row.get("row_complete")) for row in rows)
    derived_pending_ids = [row.get("blind_sample_id") for row in rows if not row.get("row_complete")]
    expected_samples = reconciled_panel.get("expected_samples", len(rows))
    completed = reconciled_panel.get("completed_blind_evaluations", derived_completed)
    pending = reconciled_panel.get("pending_blind_evaluations", len(rows) - derived_completed)
    pending_ids = reconciled_panel.get("pending_blind_sample_ids", derived_pending_ids)
    _need(len(rows) == expected_samples and completed + pending == len(rows)
          and len(pending_ids) == pending, "panel_record_count")
    _need(type(reconciled_panel["panel_sha256"]) is str and _hash(rows) == reconciled_panel["panel_sha256"],
          "panel_hash_mismatch")
    seen_samples = set()
    groups = {}
    for row in rows:
        _panel_row(row, reconciled_panel["is_demo"])
        _need(row["blind_sample_id"] not in seen_samples, "duplicate_sample_id")
        seen_samples.add(row["blind_sample_id"])
        key = (row["topic_id"], row["block_id"], row["world_id"], row["environment"], row["arm"], row["timepoint"])
        groups.setdefault(key, []).append(row)
    records = []
    for key in sorted(groups, key=_sort_key):
        members = groups[key]
        agents = [row["agent_id"] for row in members]
        _need(len(set(agents)) == len(agents), "duplicate_agent_in_network")
        observed = len(members)
        _need(observed <= expected_agents_per_world, "network_overfilled")
        missing = expected_agents_per_world - observed
        objective_complete = sum(bool(row["objective_complete"]) for row in members)
        blind_complete = sum(bool(row["row_complete"]) for row in members)
        objective_valid = missing == 0 and objective_complete == expected_agents_per_world
        blind_valid = missing == 0 and blind_complete == expected_agents_per_world
        lower_terms, upper_terms = [], []
        for row in members:
            lo, hi = _bounds_terms(row)
            lower_terms.append(lo)
            upper_terms.append(hi)
        bs_lower = _mean(lower_terms + [0.0] * missing, expected_agents_per_world)
        bs_upper = _mean(upper_terms + [1.0] * missing, expected_agents_per_world)
        record = {
            "contract": "network_observation_v1", "topic_id": key[0], "block_id": key[1],
            "world_id": key[2], "environment": key[3], "arm": key[4], "timepoint": key[5],
            "expected_agents": expected_agents_per_world, "observed_agents": observed,
            "missing_agents": missing, "objective_complete_count": objective_complete,
            "blind_complete_count": blind_complete, "valid": objective_valid, "blind_valid": blind_valid,
            "all_metrics_valid": objective_valid and blind_valid,
            "BS": None, "Acc": None, "S": None, "O": None, "U": None,
            "BS_lower_mean": bs_lower, "BS_upper_mean": bs_upper,
            "transfer": _transfer_summary(members, expected_agents_per_world),
            "source_blind_sample_ids": sorted(row["blind_sample_id"] for row in members),
            "primary_endpoint_partition": "anchor_eight_only",
            "independent_unit": "network_world_timepoint", "formal_eligible": False,
        }
        if objective_valid:
            for name in _OBJECTIVE_METRICS:
                record[name] = _mean([_finite(row[name], "network_metric_invalid") for row in members],
                                     expected_agents_per_world)
        if blind_valid:
            for name in _SUBJECTIVE_METRICS:
                record[name] = _mean([_finite(row[name], "network_metric_invalid") for row in members],
                                     expected_agents_per_world)
        records.append(record)
    _need(_hash(reconciled_panel) == before, "panel_input_mutated")
    return {
        "contract": NETWORK_AGGREGATION_VERSION,
        "source_panel_sha256": reconciled_panel["panel_sha256"],
        "source_registry_sha256": reconciled_panel.get("registry_sha256"),
        "expected_agents_per_world": expected_agents_per_world,
        "network_records": records, "network_records_sha256": _hash(records),
        "is_demo": _demo_flags(allow_demo, reconciled_panel["is_demo"]),
        "formal_eligible": False, "actual_observation_verified": False,
        "allow_demo": allow_demo,
        "independent_unit": "network_world_timepoint",
        "primary_endpoint_partition": "anchor_eight_only", "new_model_calls": 0,
        "analysis_scope": "network_level_conditional_inference_fixed_topics_only",
        "missing_data_policy": "incomplete_network_exact_means_are_none_bounds_use_fixed_expected_denominator",
    }


def _network_record(record, aggregation):
    _loose_fields(record, _NETWORK_ROW_REQUIRED_FIELDS, "network_row_fields")
    _need(record.get("contract", "network_observation_v1") == "network_observation_v1", "network_row_contract")
    _identifier(record["topic_id"], "network_topic_invalid")
    _identifier(record["block_id"], "network_block_invalid", allow_integer=True)
    for name in ("world_id", "environment"):
        _identifier(record[name], "network_identifier_invalid")
    _need(record["arm"] is None or record["arm"] in _ARMS, "network_arm_invalid")
    _need(record["timepoint"] in _POINTS, "network_timepoint_invalid")
    _need(record["expected_agents"] == aggregation["expected_agents_per_world"], "network_expected_mismatch")
    counts = (record["observed_agents"], record["missing_agents"], record["objective_complete_count"],
              record["blind_complete_count"])
    _need(all(type(v) is int and v >= 0 for v in counts), "network_count_invalid")
    _need(record["observed_agents"] <= record["expected_agents"]
          and record["missing_agents"] == record["expected_agents"] - record["observed_agents"]
          and record["objective_complete_count"] <= record["observed_agents"]
          and record["blind_complete_count"] <= record["observed_agents"], "network_count_mismatch")
    _need(type(record["valid"]) is bool and type(record["blind_valid"]) is bool, "network_flag_invalid")
    if "all_metrics_valid" in record:
        _need(type(record["all_metrics_valid"]) is bool
              and record["all_metrics_valid"] == (record["valid"] and record["blind_valid"]), "network_flag_mismatch")
    lower, upper = _unit(record["BS_lower_mean"], "network_bound_invalid"), _unit(record["BS_upper_mean"], "network_bound_invalid")
    _need(lower <= upper, "network_bounds_reversed")
    if record["valid"]:
        _need(record["BS"] is not None, "network_objective_validity_mismatch")
    else:
        _need(all(record[name] is None for name in _OBJECTIVE_METRICS), "network_objective_validity_mismatch")
    for name in _OBJECTIVE_METRICS:
        if record[name] is not None:
            _finite(record[name], "network_metric_invalid")
    if not record["blind_valid"]:
        _need(all(record[name] is None for name in _SUBJECTIVE_METRICS), "network_subjective_validity_mismatch")
    for name in _SUBJECTIVE_METRICS:
        if record[name] is not None:
            _finite(record[name], "network_metric_invalid")
    if "source_blind_sample_ids" in record:
        _need(type(record["source_blind_sample_ids"]) is list
              and len(record["source_blind_sample_ids"]) == record["observed_agents"], "network_source_ids_mismatch")
    _need(record.get("formal_eligible", False) is False
          and record.get("primary_endpoint_partition", "anchor_eight_only") == "anchor_eight_only"
          and record.get("independent_unit", "network_world_timepoint") == "network_world_timepoint", "network_row_identity")
    transfer = record.get("transfer")
    if transfer is not None:
        _need(type(transfer) is dict and transfer.get("primary_endpoint_separate", True) is True,
              "network_transfer_invalid")


def _verified_aggregation(aggregation, allow_demo):
    before = _hash(aggregation)
    _loose_fields(aggregation, _NETWORK_REQUIRED_FIELDS, "aggregation_fields")
    _need(aggregation["contract"] == NETWORK_AGGREGATION_VERSION, "aggregation_contract_invalid")
    _need(aggregation["formal_eligible"] is False
          and aggregation.get("new_model_calls", 0) == 0
          and aggregation.get("actual_observation_verified", False) is False
          and aggregation.get("independent_unit", "network_world_timepoint") == "network_world_timepoint"
          and aggregation.get("primary_endpoint_partition", "anchor_eight_only") == "anchor_eight_only",
          "aggregation_identity_invalid")
    _population_size(aggregation["expected_agents_per_world"])
    rows = aggregation["network_records"]
    _need(type(rows) is list and _hash(rows) == aggregation["network_records_sha256"], "aggregation_hash_mismatch")
    for row in rows:
        _network_record(row, aggregation)
    seen = {(row["topic_id"], row["block_id"], row["world_id"], row["environment"], row["arm"], row["timepoint"])
            for row in rows}
    _need(len(seen) == len(rows), "duplicate_network_record")
    _need(_hash(aggregation) == before, "aggregation_input_mutated")
    return _demo_flags(allow_demo, aggregation["is_demo"])


def _call_exposure_contrasts(changes, is_demo):
    payload = _ControlledDict(changes) if is_demo else dict(changes)
    if is_demo:
        payload._analysis_controlled_demo_clone = True
    return metrics.exposure_contrasts(payload)


def _record_metric(record, metric):
    if metric in _OBJECTIVE_METRICS:
        usable = record["valid"]
    else:
        usable = record["blind_valid"]
    value = record[metric]
    return usable and value is not None


def _exposure_bounds(changes_lower, changes_upper):
    a0, b0, c0, d0 = (changes_lower[key] for key in "ABCD")
    a1, b1, c1, d1 = (changes_upper[key] for key in "ABCD")
    return {
        "homogeneous": [((c0 + d0) - (a1 + b1)) / 2, ((c1 + d1) - (a0 + b0)) / 2],
        "misinformation": [((b0 + d0) - (a1 + c1)) / 2, ((b1 + d1) - (a0 + c0)) / 2],
        "interaction": [(d0 - c1) - (b1 - a0), (d1 - c0) - (b0 - a1)],
    }


@_api
def analyze_exposure_2x2(network_aggregation, *, target_topics, metric="BS", allow_demo=False) -> dict:
    """用网络级T0/T1变化计算A/B/C/D同质、错误信息与交互效应。"""
    before = _hash(network_aggregation)
    is_demo = _verified_aggregation(network_aggregation, allow_demo)
    metric = _metric_name(metric)
    available_topics = {row["topic_id"] for row in network_aggregation["network_records"]}
    topics = _target_topics(target_topics, available_topics)
    rows = [row for row in network_aggregation["network_records"]
            if row["topic_id"] in topics and row["environment"] in _ENVIRONMENTS
            and row["timepoint"] in ("T0", "T1")]
    # T1 同时可能留有干预臂网络；存在未分配臂的曝光网络时只取这些真正的前测网络。
    # 这是阶段选择，不是丢弃缺测个体：阶段内的缺测仍由网络记录 valid/bounds 保留。
    exposure_arm_scope = "all_pre_intervention_rows"
    if any(row["arm"] is None for row in rows):
        rows = [row for row in rows if row["arm"] is None]
        exposure_arm_scope = "unassigned_pre_intervention_networks_only"
    groups = {}
    for row in rows:
        key = (row["topic_id"], row["block_id"], row["environment"])
        bucket = groups.setdefault(key, {})
        _need(row["timepoint"] not in bucket, "duplicate_exposure_timepoint")
        bucket[row["timepoint"]] = row
    blocks = {(row["topic_id"], row["block_id"]) for row in rows}
    per_block, complete_by_topic = [], {topic: [] for topic in topics}
    for topic, block in sorted(blocks, key=_sort_key):
        changes, lower, upper, valid = {}, {}, {}, True
        details = {}
        for env in _ENVIRONMENTS:
            pair = groups.get((topic, block, env), {})
            t0, t1 = pair.get("T0"), pair.get("T1")
            if t0 is not None and t1 is not None:
                _need(t0["world_id"] == t1["world_id"] and t0["arm"] == t1["arm"]
                      and t0["expected_agents"] == t1["expected_agents"], "exposure_condition_mismatch")
            cell = {"T0_metric": None, "T1_metric": None, "change": None, "valid": False}
            if t0 is not None and t1 is not None and _record_metric(t0, metric) and _record_metric(t1, metric):
                value = _finite(t1[metric], "exposure_metric_invalid") - _finite(t0[metric], "exposure_metric_invalid")
                changes[env] = value
                cell.update(T0_metric=t0[metric], T1_metric=t1[metric], change=value, valid=True)
            else:
                valid = False
            if t0 is None:
                t0_lower, t0_upper = 0.0, 1.0
            else:
                t0_lower, t0_upper = t0["BS_lower_mean"], t0["BS_upper_mean"]
            if t1 is None:
                t1_lower, t1_upper = 0.0, 1.0
            else:
                t1_lower, t1_upper = t1["BS_lower_mean"], t1["BS_upper_mean"]
            lower[env], upper[env] = t1_lower - t0_upper, t1_upper - t0_lower
            details[env] = cell
        contrasts = _call_exposure_contrasts(changes, is_demo) if valid else None
        bounds = _exposure_bounds(lower, upper) if metric == "BS" else None
        entry = {"topic_id": topic, "block_id": block, "valid": valid, "cells": details,
                 "changes": changes if valid else None, "contrasts": contrasts,
                 "bounds_from_missingness": bounds}
        per_block.append(entry)
        if valid:
            complete_by_topic[topic].append({"block_id": block, "changes": changes, "bounds": bounds})
    per_topic, missing_topics, pooled_changes_by_topic = {}, [], {}
    for topic in topics:
        complete = complete_by_topic[topic]
        expected_blocks = sum(1 for t, b in blocks if t == topic)
        missing_blocks = [b for t, b in sorted(blocks, key=_sort_key) if t == topic
                          and b not in {item["block_id"] for item in complete}]
        pooled = None
        contrasts = None
        bounds = None
        if complete:
            pooled = {env: _mean([item["changes"][env] for item in complete], len(complete)) for env in _ENVIRONMENTS}
            contrasts = _call_exposure_contrasts(pooled, is_demo)
            if metric == "BS":
                bounds = {
                    name: [_mean([item["bounds"][name][0] for item in complete], len(complete)),
                           _mean([item["bounds"][name][1] for item in complete], len(complete))]
                    for name in ("homogeneous", "misinformation", "interaction")
                }
            pooled_changes_by_topic[topic] = pooled
        else:
            missing_topics.append(topic)
        per_topic[topic] = {"expected_blocks": expected_blocks, "complete_blocks": len(complete),
                            "missing_blocks": missing_blocks, "coverage": len(complete) / expected_blocks if expected_blocks else None,
                            "estimable": bool(complete), "pooled_changes": pooled, "contrasts": contrasts,
                            "bounds_from_missingness": bounds}
    overall_changes = None
    overall_contrasts = None
    overall_bounds = None
    if not missing_topics:
        overall_changes = {env: _mean([pooled_changes_by_topic[topic][env] for topic in topics], len(topics))
                           for env in _ENVIRONMENTS}
        overall_contrasts = _call_exposure_contrasts(overall_changes, is_demo)
        if metric == "BS":
            overall_bounds = {
                name: [_mean([per_topic[t]["bounds_from_missingness"][name][0] for t in topics], len(topics)),
                       _mean([per_topic[t]["bounds_from_missingness"][name][1] for t in topics], len(topics))]
                for name in ("homogeneous", "misinformation", "interaction")
            }
    _need(_hash(network_aggregation) == before, "aggregation_input_mutated")
    return {
        "contract": "exposure_2x2_v1", "metric": metric, "target_topics": topics,
        "exposure_arm_scope": exposure_arm_scope,
        "metric_direction": "d=T1-T0; lower_is_better" if not _METRICS[metric]["higher_is_better"]
                            else "d=T1-T0; higher_is_better",
        "per_block": per_block, "per_topic": per_topic, "missing_topics": missing_topics,
        "overall": {"estimable": not missing_topics, "changes": overall_changes,
                    "contrasts": overall_contrasts, "bounds_from_missingness": overall_bounds},
        "bounds_scope": "BS_lower_BS_upper_fixed_expected_denominator" if metric == "BS" else "not_applicable_non_BS_metric",
        "is_demo": is_demo, "formal_eligible": False, "actual_observation_verified": False,
        "new_model_calls": 0,
        "independent_unit": "network_world_timepoint", "source_network_sha256": network_aggregation["network_records_sha256"],
        "underlying_operator": "metrics.exposure_contrasts",
    }


def _controlled_record(record, is_demo):
    payload = _ControlledDict(record) if is_demo else dict(record)
    if is_demo:
        payload._analysis_controlled_demo_clone = True
    payload["is_demo"] = False
    return payload


def _network_lookup(aggregation, *, environment, topics, arms, timepoints):
    lookup, blocks = {}, {topic: set() for topic in topics}
    for row in aggregation["network_records"]:
        if row["topic_id"] not in topics or row["environment"] != environment:
            continue
        if row["arm"] not in arms or row["timepoint"] not in timepoints:
            continue
        blocks[row["topic_id"]].add(row["block_id"])
        key = (row["topic_id"], row["block_id"], row["arm"], row["timepoint"])
        _need(key not in lookup, "duplicate_intervention_network")
        lookup[key] = row
    return lookup, blocks


def _shared_baseline(lookup, blocks, metric):
    baselines = {}
    for topic in blocks:
        for block in sorted(blocks[topic], key=lambda value: (isinstance(value, str), value)):
            values = []
            for arm in _ARMS:
                row = lookup.get((topic, block, arm, "T1"))
                if row is not None and _record_metric(row, metric):
                    values.append(_metric_loss(metric, row[metric]))
            if values:
                low, high = min(values), max(values)
                _need(high - low <= 1e-12, "shared_T1_mismatch")
                baselines[(topic, block)] = _mean(values, len(values))
    return baselines


def _paired_records(lookup, blocks, metric, endpoint, baselines, is_demo):
    records = []
    for topic in blocks:
        for block in sorted(blocks[topic], key=lambda value: (isinstance(value, str), value)):
            shared = baselines.get((topic, block))
            for arm in _ARMS:
                t1 = lookup.get((topic, block, arm, "T1"))
                end = lookup.get((topic, block, arm, endpoint))
                valid = (shared is not None and t1 is not None and end is not None
                         and _record_metric(t1, metric) and _record_metric(end, metric))
                record = {"topic_id": topic, "block_id": block, "arm": arm, "valid": valid,
                          "T1": shared if valid else None,
                          "T3": _metric_loss(metric, end[metric]) if valid else None,
                          "is_demo": False, "endpoint_timepoint": endpoint}
                records.append(_controlled_record(record, is_demo))
    return records


def _scale_comparison(comparison, metric, family, name=None):
    result = deepcopy(comparison)
    scale = _METRICS[metric]["scale"]
    result["comparison_family"] = family
    result["metric"] = metric
    result["metric_scale"] = scale
    result["paired_summary_effect_normalized_loss"] = result.get("effect")
    result["effect"] = None if result.get("effect") is None else result["effect"] * scale
    if result.get("ci_95") is not None:
        result["ci_95"] = [v * scale for v in result["ci_95"]]
    for side in ("control", "treatment"):
        if type(result.get(side)) is dict:
            result[side] = {key: None if value is None else value * scale
                            for key, value in result[side].items()}
    if type(result.get("per_topic")) is dict:
        for row in result["per_topic"].values():
            row["paired_summary_effect_normalized_loss"] = row.get("effect")
            row["effect"] = None if row.get("effect") is None else row["effect"] * scale
            for side in ("control", "treatment"):
                if type(row.get(side)) is dict:
                    row[side] = {key: None if value is None else value * scale
                                 for key, value in row[side].items()}
    result["effect_direction"] = "positive_means_treatment_improved_more"
    result["benefit_formula"] = ("control_loss_change_minus_treatment_loss_change; loss=metric for lower-is-better"
                                 if not _METRICS[metric]["higher_is_better"] else
                                 "control_loss_change_minus_treatment_loss_change; loss=max-metric for higher-is-better")
    if name is not None:
        result["comparison_id"] = name
    return result


def _secondary_comparison(records, target_topics, control, treatment, metric, bootstrap_reps, seed, index, is_demo):
    relabeled = []
    for record in records:
        if record["arm"] not in (control, treatment):
            continue
        row = dict(record)
        row["arm"] = "I0" if row["arm"] == control else "I4"
        relabeled.append(_controlled_record(row, is_demo))
    raw = metrics.paired_summary(relabeled, target_topics, bootstrap_reps, seed + index)
    return _scale_comparison(raw["comparisons"]["I4_vs_I0"], metric, "secondary_exploratory",
                             f"{treatment}_vs_{control}")


def _allocation_rows(item, code):
    rows = item.get("rows", item.get("allocation", item.get("slots")))
    _need(type(rows) is list and len(rows) == 6, code)
    return rows


def _normalize_allocations(allocations):
    _need(allocations is not None, "allocations_invalid")
    canonical_arms = list(_ARMS)
    if type(allocations) is dict:
        single = "blocks" not in allocations
        required = ("topic_id", "block_id") if single else ("blocks",)
        _loose_fields(allocations, required, "allocation_registry_fields")
        if single:
            arms = allocations.get("arms", canonical_arms)
            seed = allocations.get("seed", allocations.get("allocation_seed"))
            _need(type(seed) is int, "allocation_seed_invalid")
            _need(type(arms) is list and set(arms) == set(canonical_arms) and len(arms) == 6,
                  "allocation_arms_invalid")
            return [{"topic_id": allocations["topic_id"], "block_id": allocations["block_id"],
                     "seed": seed, "rows": _allocation_rows(allocations, "allocation_rows_invalid"),
                     "arms": arms}]
        _need(allocations.get("contract", "allocation_registry_v1") == "allocation_registry_v1",
              "allocation_contract_invalid")
        arms = allocations.get("arms", canonical_arms)
        _need(type(arms) is list and set(arms) == set(canonical_arms) and len(arms) == 6,
              "allocation_arms_invalid")
        raw_blocks = allocations["blocks"]
        _need(type(raw_blocks) is list and bool(raw_blocks), "allocation_blocks_invalid")
        global_seed = allocations.get("seed", allocations.get("allocation_seed"))
        normalized = []
        for item in raw_blocks:
            _loose_fields(item, ("topic_id", "block_id"), "allocation_block_fields")
            block_seed = item.get("seed", item.get("allocation_seed", global_seed))
            _need(type(block_seed) is int, "allocation_seed_invalid")
            normalized.append({"topic_id": item["topic_id"], "block_id": item["block_id"],
                               "seed": block_seed, "rows": _allocation_rows(item, "allocation_rows_invalid"),
                               "arms": arms})
        return normalized
    _need(type(allocations) is list and bool(allocations), "allocations_invalid")
    block_keys = ("rows", "allocation", "slots")
    if all(type(item) is dict and any(key in item for key in block_keys) for item in allocations):
        normalized = []
        for item in allocations:
            _loose_fields(item, ("topic_id", "block_id"), "allocation_block_fields")
            block_seed = item.get("seed", item.get("allocation_seed"))
            _need(type(block_seed) is int, "allocation_seed_invalid")
            normalized.append({"topic_id": item["topic_id"], "block_id": item["block_id"],
                               "seed": block_seed, "rows": _allocation_rows(item, "allocation_rows_invalid"),
                               "arms": canonical_arms})
        return normalized
    grouped = {}
    for item in allocations:
        _loose_fields(item, ("topic_id", "block_id", "slot", "arm", "response_seed"),
                      "allocation_flat_row_fields")
        flat_seed = item.get("seed", item.get("allocation_seed"))
        _need(type(flat_seed) is int, "allocation_seed_invalid")
        key = (item["topic_id"], item["block_id"], flat_seed)
        row = dict(item)
        row["seed"] = flat_seed
        grouped.setdefault(key, []).append(row)
    normalized = []
    for key, rows in grouped.items():
        normalized.append({"topic_id": key[0], "block_id": key[1], "seed": key[2],
                           "rows": rows, "arms": canonical_arms})
    return normalized


def _validate_allocations(allocations, lookup, blocks, environment, target_topics, endpoint, metric, baselines):
    normalized = _normalize_allocations(allocations)
    registry_blocks = {}
    for block in normalized:
        _identifier(block["topic_id"], "allocation_topic_invalid")
        _identifier(block["block_id"], "allocation_block_invalid", allow_integer=True)
        _need(block["topic_id"] in target_topics, "allocation_unregistered_topic")
        _need(type(block["seed"]) is int, "allocation_seed_invalid")
        rows = block["rows"]
        _need(type(rows) is list and len(rows) == 6, "allocation_rows_invalid")
        key = (block["topic_id"], block["block_id"])
        _need(key not in registry_blocks, "duplicate_allocation_block")
        generated = scheduling.allocate(block["arms"], block["seed"])
        checked = []
        for row in rows:
            _loose_fields(row, ("slot", "arm", "response_seed"), "allocation_row_fields")
            _need(type(row["slot"]) is int and 0 <= row["slot"] < 6
                  and row["arm"] in block["arms"] and type(row["response_seed"]) is int,
                  "allocation_row_invalid")
            checked.append({"slot": row["slot"], "arm": row["arm"], "response_seed": row["response_seed"]})
        _need(sorted(checked, key=lambda x: x["slot"]) == sorted(generated, key=lambda x: x["slot"]),
              "allocation_not_generated_by_scheduling_allocate")
        by_slot = sorted(rows, key=lambda x: x["slot"])
        _need([r["arm"] for r in by_slot] == [r["arm"] for r in sorted(generated, key=lambda x: x["slot"])],
              "allocation_label_order_mismatch")
        _need(len({r["arm"] for r in by_slot}) == 6, "allocation_duplicate_arm")
        registry_blocks[key] = {"seed": block["seed"], "arms": list(block["arms"]),
                                "rows": [{"slot": r["slot"], "arm": r["arm"], "response_seed": r["response_seed"],
                                          "world_id": r.get("world_id")} for r in by_slot]}
    network_blocks = {(topic, block) for topic in blocks for block in blocks[topic]}
    _need(network_blocks == set(registry_blocks), "allocation_block_set_mismatch")
    network_arm_keys = {(topic, block, arm) for topic, block, arm, _ in lookup}
    allocation_arm_keys = {(topic, block, row["arm"])
                           for (topic, block), data in registry_blocks.items() for row in data["rows"]}
    _need(network_arm_keys == allocation_arm_keys, "allocation_arm_set_mismatch")
    complete_blocks, observed = [], []
    for key in sorted(registry_blocks, key=_sort_key):
        topic, block = key
        row_info = registry_blocks[key]
        labels, changes, endpoints, valid = [], [], [], True
        for slot, allocation in enumerate(row_info["rows"]):
            arm = allocation["arm"]
            t1 = lookup.get((topic, block, arm, "T1"))
            end = lookup.get((topic, block, arm, endpoint))
            network_world = allocation.get("world_id")
            for record in (t1, end):
                _need(record is not None, "allocation_missing_observation")
                if network_world is None:
                    network_world = record["world_id"]
                _need(record["world_id"] == network_world and record["environment"] == environment,
                      "allocation_observation_world_mismatch")
            _need(t1["world_id"] == end["world_id"], "allocation_T1_endpoint_world_mismatch")
            labels.append(arm)
            endpoints.append({"arm": arm, "T1": t1, "endpoint": end})
            if not (_record_metric(t1, metric) and _record_metric(end, metric)):
                valid = False
                changes.append(None)
            else:
                changes.append(_metric_loss(metric, end[metric]) - _metric_loss(metric, t1[metric]))
        baseline = baselines.get((topic, block))
        if baseline is None:
            valid = False
        complete = {"topic_id": topic, "block_id": block, "seed": row_info["seed"],
                    "labels": labels, "changes": changes, "valid": valid, "observations": endpoints}
        observed.append(complete)
        if valid:
            complete_blocks.append(complete)
    return {"contract": "validated_allocation_registry_v1", "blocks": observed,
            "complete_blocks": complete_blocks, "registry_sha256": _hash(observed),
            "environment": environment, "endpoint_timepoint": endpoint, "metric": metric}


def _observed_statistic(blocks, control, topics):
    by_topic = {topic: [] for topic in topics}
    for block in blocks:
        by_arm = dict(zip(block["labels"], block["changes"]))
        by_topic[block["topic_id"]].append(by_arm[control] - by_arm["I4"])
    _need(all(by_topic[topic] for topic in topics), "permutation_topic_without_complete_block")
    return _mean([_mean(by_topic[topic], len(by_topic[topic])) for topic in topics], len(topics))


def _randomization_pvalues(allocation, comparisons, permutation_reps, seed, topics, metric):
    blocks = allocation["complete_blocks"]
    if not blocks or not all(comparisons[name].get("estimable") for name in ("I4_vs_I0", "I4_vs_I5")):
        return None, None, {"reason": "insufficient_complete_valid_blocks_or_fixed_topic_pairs"}
    observed = {}
    for name, control in (("I4_vs_I0", "I0"), ("I4_vs_I5", "I5")):
        raw = comparisons[name].get("paired_summary_effect_normalized_loss")
        _need(raw is not None, "observed_effect_missing")
        checked = _observed_statistic(blocks, control, topics)
        _need(abs(checked - raw) <= 1e-9, "observed_statistic_mismatch")
        observed[name] = raw
    rng = random.Random(seed)
    counts = {name: 0 for name in observed}
    controls = (("I4_vs_I0", "I0"), ("I4_vs_I5", "I5"))
    for _ in range(permutation_reps):
        # 两项确认性比较各自保留独立的置换统计量；同一组标签重排，不混算两条差。
        per_topic = {name: {topic: [] for topic in topics} for name in observed}
        for block in blocks:
            labels = list(block["labels"])
            rng.shuffle(labels)
            by_arm = dict(zip(labels, block["changes"]))
            for name, control in controls:
                per_topic[name][block["topic_id"]].append(by_arm[control] - by_arm["I4"])
        for name in observed:
            strata = per_topic[name]
            stat = _mean([_mean(strata[topic], len(strata[topic])) for topic in topics], len(topics))
            if abs(stat) >= abs(observed[name]) - 1e-12:
                counts[name] += 1
    p_raw = {name: (1 + counts[name]) / (1 + permutation_reps) for name in observed}
    p_adjusted = metrics.holm(dict(p_raw))
    detail = {"method": "within_block_six_arm_slot_label_permutation",
              "p_formula": "(1+count(abs(stat_perm)>=abs(stat_obs)))/(1+permutation_reps)",
              "permutation_reps": permutation_reps, "seed": seed,
              "complete_blocks": len(blocks), "observed_statistics": observed,
              "extreme_counts": counts, "holm_operator": "metrics.holm"}
    return p_raw, p_adjusted, detail


def _intervention_bounds(lookup, blocks, endpoint, topics):
    def arm_change_bounds(topic, block, arm):
        t1 = lookup.get((topic, block, arm, "T1"))
        end = lookup.get((topic, block, arm, endpoint))
        t1_low, t1_high = (0.0, 1.0) if t1 is None else (t1["BS_lower_mean"], t1["BS_upper_mean"])
        end_low, end_high = (0.0, 1.0) if end is None else (end["BS_lower_mean"], end["BS_upper_mean"])
        return end_low - t1_high, end_high - t1_low
    per_topic, overall = {}, {}
    for name, control, treatment in (("I4_vs_I0", "I0", "I4"), ("I4_vs_I5", "I5", "I4")):
        overall[name] = {"lower": [], "upper": []}
    for topic in topics:
        topic_bounds = {}
        for name, control, treatment in (("I4_vs_I0", "I0", "I4"), ("I4_vs_I5", "I5", "I4")):
            lows, highs = [], []
            for block in sorted(blocks[topic], key=lambda value: (isinstance(value, str), value)):
                c_low, c_high = arm_change_bounds(topic, block, control)
                t_low, t_high = arm_change_bounds(topic, block, treatment)
                lows.append(c_low - t_high)
                highs.append(c_high - t_low)
            if not lows:
                topic_bounds[name] = {"lower": None, "upper": None, "expected_blocks": 0}
            else:
                low, high = _mean(lows, len(lows)), _mean(highs, len(highs))
                topic_bounds[name] = {"lower": low, "upper": high, "expected_blocks": len(lows)}
                overall[name]["lower"].append(low)
                overall[name]["upper"].append(high)
        per_topic[topic] = topic_bounds
    for name in overall:
        lows, highs = overall[name]["lower"], overall[name]["upper"]
        overall[name] = {"lower": _mean(lows, len(lows)) if lows else None,
                         "upper": _mean(highs, len(highs)) if highs else None,
                         "expected_topics": len(lows)}
    return {"metric": "BS", "scope": "BS_lower_BS_upper_missingness_sensitivity_only",
            "per_topic": per_topic, "overall": overall}


@_api
def analyze_intervention_effects(network_aggregation, *, target_topics, allocations=None,
                                 endpoint_timepoint="T3", metric="BS", bootstrap_reps=2000,
                                 permutation_reps=2000, seed=0, allow_demo=False,
                                 environment="D") -> dict:
    """主/次干预配对效应与实际allocation区组内随机化检验；不伪造p值。"""
    before = _hash(network_aggregation)
    _need(type(endpoint_timepoint) is str and endpoint_timepoint in ("T2", "T3"), "endpoint_invalid")
    _need(type(bootstrap_reps) is int and bootstrap_reps > 0, "bootstrap_reps_invalid")
    _need(type(permutation_reps) is int and permutation_reps > 0, "permutation_reps_invalid")
    _need(type(seed) is int, "seed_invalid")
    _identifier(environment, "environment_invalid")
    is_demo = _verified_aggregation(network_aggregation, allow_demo)
    metric = _metric_name(metric)
    topics = _target_topics(target_topics, {row["topic_id"] for row in network_aggregation["network_records"]})
    lookup, blocks = _network_lookup(network_aggregation, environment=environment, topics=topics,
                                     arms=_ARMS, timepoints=("T1", endpoint_timepoint))
    baselines = _shared_baseline(lookup, blocks, metric)
    records = _paired_records(lookup, blocks, metric, endpoint_timepoint, baselines, is_demo)
    allocation_info = None
    allocation_before = None
    allocation_records_restricted = False
    if allocations is not None:
        allocation_before = _hash(allocations)
        allocation_info = _validate_allocations(allocations, lookup, blocks, environment, topics,
                                                endpoint_timepoint, metric, baselines)
        complete_keys = {(item["topic_id"], item["block_id"])
                         for item in allocation_info["complete_blocks"]}
        # 有实际槽位分配时，确认性估计与随机化检验必须共用同一批完整有效区组；
        # 仅把其余区组标为无效配对，仍留在 expected_n 分母中，不删除记录。
        restricted = []
        for record in records:
            row = dict(record)
            if (row["topic_id"], row["block_id"]) not in complete_keys:
                row["valid"] = False
            restricted.append(_controlled_record(row, is_demo))
        records = restricted
        allocation_records_restricted = True
    raw = metrics.paired_summary(records, topics, bootstrap_reps, seed)
    comparisons = {
        "I4_vs_I0": _scale_comparison(raw["comparisons"]["I4_vs_I0"], metric, "primary_confirmatory", "I4_vs_I0"),
        "I4_vs_I5": _scale_comparison(raw["comparisons"]["I4_vs_I5"], metric, "primary_confirmatory", "I4_vs_I5"),
    }
    secondary = {}
    for index, treatment in enumerate(("I1", "I2", "I3", "I5"), 1):
        key = f"{treatment}_vs_I0"
        secondary[key] = _secondary_comparison(records, topics, "I0", treatment, metric,
                                               bootstrap_reps, seed, index, is_demo)
    p_raw = {name: None for name in comparisons}
    p_adjusted = {name: None for name in comparisons}
    p_detail = None
    if allocation_info is not None:
        computed_raw, computed_adjusted, p_detail = _randomization_pvalues(
            allocation_info, comparisons, permutation_reps, seed, topics, metric)
        if computed_raw is not None:
            p_raw, p_adjusted = computed_raw, computed_adjusted
        _need(_hash(allocations) == allocation_before, "allocation_input_mutated")
    for name in comparisons:
        comparisons[name]["p_raw"] = p_raw[name]
        comparisons[name]["p_adjusted"] = p_adjusted[name]
        comparisons[name]["p_value_status"] = "computed_from_actual_allocation" if p_raw[name] is not None else "not_computed"
    for item in secondary.values():
        item["p_raw"] = None
        item["p_adjusted"] = None
        item["p_value_status"] = "secondary_exploratory_no_confirmatory_test"
    if allocations is None:
        p_note = raw["p_value_note"]
    elif not all(value is not None for value in p_raw.values()):
        p_note = "已提供allocation但完整有效配对区组不足，p_raw/p_adjusted保持None，不伪造随机化检验。"
    else:
        p_note = ("基于实际scheduling.allocate槽位分配，在每个完整有效区组内重排六臂标签；"
                  "Phipson-Smyth保守双侧公式，两项确认性比较用metrics.holm校正。")
        if (allocation_info is not None
                and len(allocation_info["complete_blocks"]) != len(allocation_info["blocks"])):
            p_note += "存在缺测区组；该p值仅对完整有效区组作条件推断，覆盖率不是全实验覆盖率。"
    bounds = (_intervention_bounds(lookup, blocks, endpoint_timepoint, topics)
              if metric == "BS" else {"metric": metric, "scope": "not_applicable_non_BS_metric",
                                      "per_topic": {}, "overall": {}})
    coverage = {name: {"n": item["n"], "expected_n": item["expected_n"],
                       "count_missing": item["count_missing"], "coverage": item["coverage"],
                       "per_topic": item["per_topic"]} for name, item in comparisons.items()}
    _need(_hash(network_aggregation) == before, "aggregation_input_mutated")
    return {
        "contract": "intervention_effects_v1", "metric": metric, "endpoint_timepoint": endpoint_timepoint,
        "environment": environment, "target_topics": topics, "metric_scale": _METRICS[metric]["scale"],
        "higher_is_better": _METRICS[metric]["higher_is_better"],
        "effect_definition": "positive_means_treatment_improved_more_after_normalized_loss_transform",
        "comparisons": comparisons, "secondary_comparisons": secondary,
        "all_comparisons": {**comparisons, **secondary},
        "paired_summary_raw": raw, "coverage": coverage,
        "confirmatory_records_restricted_to_complete_allocation_blocks": allocation_records_restricted,
        "bounds_from_missingness": bounds, "p_raw": p_raw, "p_adjusted": p_adjusted,
        "randomization": p_detail, "allocation_validation": None if allocation_info is None else {
            "contract": allocation_info["contract"], "registry_sha256": allocation_info["registry_sha256"],
            "registered_blocks": len(allocation_info["blocks"]),
            "complete_blocks": len(allocation_info["complete_blocks"]),
            "environment": environment, "endpoint_timepoint": endpoint_timepoint,
        },
        "p_value_note": p_note,
        "randomization_scope": (None if allocation_info is None else
                                "all_registered_blocks_complete"
                                if len(allocation_info["complete_blocks"]) == len(allocation_info["blocks"])
                                else "conditional_on_complete_blocks_only"),
        "bootstrap_reps": bootstrap_reps, "permutation_reps": permutation_reps,
        "seed": seed, "is_demo": is_demo, "formal_eligible": False, "new_model_calls": 0,
        "source_network_sha256": network_aggregation["network_records_sha256"],
        "independent_unit": "paired_network_block_not_individual_agent",
        "analysis_scope": "fixed_topic_conditional_inference_no_topic_generalization",
    }
