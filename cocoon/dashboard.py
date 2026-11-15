"""步骤37：三议题全链路可视化看板与离线结案交付包。

按《破茧助手全量实验方案_v1.md》§12.1–12.4 与 §16，把步骤34–36 已验证的数据
导出为：可复核的派生数据包（dashboard_data.json）、零外部依赖的单文件只读看板
（dashboard.html）与 §12.4 验收核验（verification）。

诚实铁律（页面顶端持续横幅 + 数据包字段）：
- 这是**离线系统预演与方案A核验看板**，不是真实模型认知实验结果；
- `is_demo=True`、`actual_model_calls=0`、`formal_eligible=False`；
- 严格区分真实取证来源、摘编材料、方案A代理签发与确定性合成预演响应。

本模块不联网、不调用模型、不写原始日志；只读既有取证/审阅产物。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import random
import re

from . import ai_assisted_review as aar, analysis, human_review as hr, live_cross_topic_delivery as lcd
from . import closed_world_pilot as cwp, live_micro_pilot as lmp
from . import metrics, pilot_materials as pm
from . import scheduling, system_rehearsal as sr

__all__ = ["build_dashboard_dataset", "render_experiment_dashboard",
           "verify_dashboard_bundle"]

DATASET_VERSION = "experiment_dashboard_v1"
VERIFICATION_VERSION = "experiment_dashboard_verification_v1"
BANNER_TEXT = (
    "三议题全链路离线系统预演与方案A核验看板（示例/演练数据，非真实模型认知实验结果；"
    "is_demo=True, actual_model_calls=0, formal_eligible=False）"
)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "experiment/dashboard_20261110"
DEFAULT_LEDGER = ROOT / "experiment/billing/mimo_initial_probe_v1.jsonl"
DEFAULT_VALIDATION_DIR = ROOT / "experiment/review"
DEFAULT_DELIVERY_DIR = ROOT / "experiment/delivery_probes"
DEFAULT_EXPORT_PATH = ROOT / "experiment/exports/cocoon_closeout_dashboard_20261110.zip"
PROTOCOL_PATH = ROOT / "破茧助手全量实验方案_v1.md"

VIEWS = (
    ("view-01", "1. 实验总览", "议题×区组×环境矩阵、方案A签发状态、调用与成本、失败留痕"),
    ("view-02", "2. 同步时间轴与阶段步进", "T0→8轮曝光→T1→六臂分支→2轮干预→T2→4轮保持→T3"),
    ("view-03", "3. 网络回放视图", "8人基础网络固定坐标拓扑；同立场2/异立场2关注关系"),
    ("view-04", "4. 信息流对照", "A/B/C/D 四环境并排材料流与 X、Hn、错误卡比例"),
    ("view-05", "5. 个体四时点轨迹", "概率/BS 轨迹、答错高信心 S 标记、三类典型案例"),
    ("view-06", "6. 六臂干预并排对照", "I0–I5 可见文本、600–800 字符、I4/I5 共同证据块"),
    ("view-07", "7. 证据追踪可点击链", "来源→证据→卡片→受试引用→回答→盲评→网络"),
    ("view-08", "8. 群体结果与效应图", "2×2 曝光效应与六臂配对效应森林图"),
    ("view-09", "9. 置信度校准视图", "正误×信心分布；锚题8题与迁移题4题分母分离"),
    ("view-10", "10. 六维覆盖与异见理解", "掌握率/识别缺口率/遗漏率 O 与 U 三分项"),
    ("view-11", "11. 系统质量与方案A治理审计", "引用通过率、4项争议裁决、权利预警、§15.1门禁"),
)

_FORBIDDEN_LOAD = (
    re.compile(r"<script[^>]+src\s*=", re.I),
    re.compile(r"<link[^>]+href\s*=", re.I),
    re.compile(r"@import\s", re.I),
    re.compile(r"url\(\s*['\"]?https?:", re.I),
    re.compile(r"\bfetch\s*\(", re.I),
    re.compile(r"XMLHttpRequest", re.I),
    re.compile(r"<img[^>]+src\s*=\s*['\"]https?:", re.I),
    re.compile(r"<iframe", re.I),
)
_CREDENTIAL = re.compile(
    r"sk-[A-Za-z0-9]{8,}|api[_-]?key\s*[:=]|Authorization\s*:|Bearer\s+[A-Za-z0-9._-]{10,}",
    re.I)


def _need(condition, code):
    if not condition:
        raise ValueError("dashboard:" + code)


def _sha(value):
    return sr._sha(value)


def _bytes_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_build(evidence_root):
    path = Path(evidence_root).resolve()
    _need(path.exists(), "evidence_root_missing")
    if (path / "manifest.json").is_file():
        return path
    candidates = sorted(p for p in path.glob("build_*") if (p / "manifest.json").is_file())
    _need(len(candidates) == 1, "evidence_build_not_unique")
    return candidates[0]


# --------------------------------------------------------------- 账本与里程碑
def _billing_summary(ledger_path):
    path = Path(ledger_path)
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    settles = [row for row in rows if row.get("type") == "settle"]
    total = sum((Decimal(row["charged_cny"]) for row in settles), Decimal("0"))
    initial = Decimal(str(rows[0].get("limit_cny", "5.00"))) if rows else Decimal("5.00")
    raises = [row for row in rows if row.get("type") == "raise_limit"]
    # 步骤41：有效上限取最后一次受控提额（raise_limit），init 仍保留原始授权额度。
    limit = Decimal(raises[-1]["new_limit_cny"]) if raises else initial
    requests = {row.get("request_id", "").split(":")[0] for row in settles}
    settled_ids = {row.get("request_id") for row in settles}
    held = [row for row in rows if row.get("type") == "reserve" and row.get("request_id") not in settled_ids]
    held_amount = sum((Decimal(row["reserved_cny"]) for row in held), Decimal("0"))
    return {
        "ledger_path": str(path.relative_to(ROOT)).replace("\\", "/") if path.is_file() else None,
        "ledger_sha256": _file_sha(path) if path.is_file() else None,
        "settled_calls": len(settles),
        "request_groups": len(requests),
        "total_charged_cny": str(total),
        "authorized_limit_cny": str(limit),
        "initial_limit_cny": str(initial),
        "limit_raises": [{"seq": row["seq"], "previous_limit_cny": row["previous_limit_cny"],
                          "new_limit_cny": row["new_limit_cny"], "reason": row["reason"]} for row in raises],
        "remaining_cny": str(limit - total),
        "held_unsettled_reserved_cny": str(held_amount),
        "available_after_held_cny": str(limit - total - held_amount),
        "uncertain_charges": sum(1 for row in rows if row.get("type") == "uncertain"),
        "note": "本机保守记账，非平台最终账单；新增看板/回归/方案A均为离线，费用 ¥0。",
    }


def _validation_milestones(validation_dir):
    rows = []
    for path in sorted(Path(validation_dir).glob("validation_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        rows.append({
            "file": path.name,
            "created_at_utc": data.get("created_at_utc"),
            "tests_run": data.get("tests_run"),
            "failures": data.get("failures"),
            "errors": data.get("errors"),
            "skipped": data.get("skipped"),
            "success": data.get("success"),
        })
    return rows[-8:]


def _delivery_failures(delivery_dir):
    rows = []
    for path in sorted(Path(delivery_dir).glob("*/summary.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if data.get("status") == "delivery_failed":
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            detail = error.get("safe_details") or data.get("safe_details") or {}
            rows.append({
                "run_id": path.parent.name,
                "status": data.get("status"),
                "error_code": error.get("code") or data.get("code") or data.get("error_code"),
                "detail": {key: detail[key] for key in ("len", "min", "max") if key in detail},
                "retained": True,
            })
    return rows


# --------------------------------------------------------------- 样本重建
def _unique_cards(schedule):
    seen, order = set(), []
    for batch in schedule:
        for card in batch:
            if card["card_id"] not in seen:
                seen.add(card["card_id"])
                order.append(card["card_id"])
    return order


def _rebuild_samples(bundle, parameters, rehearsal):
    """按 system_rehearsal 的确定性规则重建 1248 条样本规格，并与预演结果逐条对账。"""
    seed = parameters["seed"]
    blocks = parameters["blocks_per_topic"]
    agents = parameters["agents_per_world"]
    samples, populations, networks = [], {}, {}
    for topic_index, topic_id in enumerate(sr.TOPIC_ORDER):
        public = bundle["topics"][topic_id]["zones"]["zone_1_subject_public"]
        exposure_cards, retention_cards = public["exposure_cards"], public["retention_cards"]
        tag = "T" + topic_id.rsplit("_", 1)[-1]
        for block_index in range(blocks):
            block_id = f"B{block_index + 1:02d}"
            block_seed = seed + topic_index * 10007 + block_index * 101
            population = scheduling.make_population(agents, block_seed)
            populations[(topic_id, block_id)] = population
            networks[(topic_id, block_id)] = scheduling.make_network(population, block_seed + 1)
            vision = {}
            for environment in sr.ENVIRONMENTS:
                schedules = {
                    agent["agent_id"]: scheduling.schedule_cards(
                        exposure_cards, agent["initial_stance"], environment,
                        sr.EXPOSURE_ROUNDS, (block_seed + index) % 6, max_repeats=2)
                    for index, agent in enumerate(population)}
                for agent in population:
                    samples.append({
                        "topic_id": topic_id, "block_id": block_id,
                        "world_id": f"W_{tag}_{block_id}_{environment}",
                        "environment": environment, "arm": None, "timepoint": "T0",
                        "condition_tag": f"E{environment}", "visible_card_ids": [],
                        "agent_id": agent["agent_id"], "exposure_schedule": None,
                    })
                visible = {agent["agent_id"]: _unique_cards(schedules[agent["agent_id"]])
                           for agent in population}
                vision[environment] = visible
                for agent in population:
                    samples.append({
                        "topic_id": topic_id, "block_id": block_id,
                        "world_id": f"W_{tag}_{block_id}_{environment}",
                        "environment": environment, "arm": None, "timepoint": "T1",
                        "condition_tag": "D" if environment == "D" else f"E{environment}",
                        "visible_card_ids": list(visible[agent["agent_id"]]),
                        "agent_id": agent["agent_id"], "exposure_schedule": schedules[agent["agent_id"]],
                    })
            parent_visible = vision["D"]
            allocation = scheduling.allocate(list(sr.ARMS), block_seed + 777)
            for row in allocation:
                arm = row["arm"]
                world_id = f"W_{tag}_{block_id}_D_{arm}"
                for agent in population:
                    samples.append({
                        "topic_id": topic_id, "block_id": block_id, "world_id": world_id,
                        "environment": "D", "arm": arm, "timepoint": "T1",
                        "condition_tag": "D",
                        "visible_card_ids": list(parent_visible[agent["agent_id"]]),
                        "agent_id": agent["agent_id"], "exposure_schedule": None,
                    })
                    samples.append({
                        "topic_id": topic_id, "block_id": block_id, "world_id": world_id,
                        "environment": "D", "arm": arm, "timepoint": "T2",
                        "condition_tag": f"A{arm}",
                        "visible_card_ids": list(parent_visible[agent["agent_id"]]),
                        "agent_id": agent["agent_id"], "exposure_schedule": None,
                    })
                retention = {
                    agent["agent_id"]: scheduling.schedule_cards(
                        retention_cards, agent["initial_stance"], "D", sr.RETENTION_ROUNDS,
                        (block_seed + index + 3) % 6, max_repeats=2)
                    for index, agent in enumerate(population)}
                extended = {}
                for agent in population:
                    agent_id = agent["agent_id"]
                    merged = list(parent_visible[agent_id])
                    seen = set(merged)
                    for batch in retention[agent_id]:
                        for card in batch:
                            if card["card_id"] not in seen:
                                seen.add(card["card_id"])
                                merged.append(card["card_id"])
                    extended[agent_id] = merged
                for agent in population:
                    samples.append({
                        "topic_id": topic_id, "block_id": block_id, "world_id": world_id,
                        "environment": "D", "arm": arm, "timepoint": "T3",
                        "condition_tag": f"A{arm}",
                        "visible_card_ids": list(extended[agent["agent_id"]]),
                        "agent_id": agent["agent_id"], "exposure_schedule": None,
                    })
    # 与预演结果核对：样本数、SID 置换、逐条 payload 哈希、U 三分项。
    panel = rehearsal["analysis"]["reconciled_panel"]["panel_records"]
    _need(len(samples) == len(panel) == rehearsal["stage4_measurement"]["expected_samples"],
          "sample_count_mismatch")
    order = list(range(len(samples)))
    random.Random(seed).shuffle(order)
    sid_to_index = {f"BLIND_{slot:04d}": order[slot - 1] for slot in range(1, len(samples) + 1)}
    _need(len({row["blind_sample_id"] for row in panel}) == len(samples), "duplicate_sid")
    questionnaires = {tid: bundle["topics"][tid]["zones"]["zone_3_evaluator_private_gold"]
                      ["public_questionnaire"] for tid in sr.TOPIC_ORDER}
    golds = {tid: bundle["topics"][tid]["zones"]["zone_3_evaluator_private_gold"]
             ["private_gold_bundle"] for tid in sr.TOPIC_ORDER}
    enriched = {}
    for row in panel:
        sid = row["blind_sample_id"]
        index = sid_to_index[sid]
        spec = samples[index]
        key = (spec["topic_id"], spec["block_id"], spec["world_id"], spec["environment"],
               spec["arm"], spec["timepoint"], spec["agent_id"])
        _need(key == (row["topic_id"], row["block_id"], row["world_id"], row["environment"],
                      row["arm"], row["timepoint"], row["agent_id"]), "sid_tuple_mismatch")
        measure_spec = {name: spec[name] for name in
                        ("topic_id", "timepoint", "condition_tag", "visible_card_ids")}
        output = sr._synthetic_measurement(measure_spec, questionnaires[spec["topic_id"]])
        _need(_sha(output) == row["measurement_output_sha256"], "synthetic_output_hash_mismatch")
        digest = _sha([spec["topic_id"], spec["timepoint"], spec["condition_tag"], sid])
        parts = [int(digest[i], 16) % 3 for i in (12, 13, 14)]
        _need(abs(100 * sum(parts) / 6 - row["U"]) <= 1e-9, "u_parts_mismatch")
        enriched[sid] = {
            "index": index, "spec": spec, "output": output, "u_parts": parts,
            "gold": golds[spec["topic_id"]],
        }
    return samples, sid_to_index, enriched, populations, networks


def _panel_rows(panel, enriched):
    rows = []
    for row in panel:
        sid = row["blind_sample_id"]
        info = enriched[sid]
        rows.append({
            "sid": sid, "topic": row["topic_id"], "block": row["block_id"],
            "world": row["world_id"], "env": row["environment"], "arm": row["arm"],
            "tp": row["timepoint"], "agent": row["agent_id"],
            "BS": row["BS"], "Acc": row["Acc"], "S": row["S"], "O": row["O"], "U": row["U"],
            "lo": row["BS_lower"], "hi": row["BS_upper"],
            "ok": bool(row["row_complete"]),
            "msha": row["measurement_output_sha256"],
            "u_parts": info["u_parts"],
            "mastered_rate": row["coverage"]["mastered_rate"],
            "gap_rate": row["coverage"]["recognized_gap_rate"],
            "visible_count": len(info["spec"]["visible_card_ids"]),
            "condition_tag": info["spec"]["condition_tag"],
        })
    rows.sort(key=lambda item: (item["topic"], item["block"], item["world"], item["tp"], item["agent"]))
    return rows


def _series(panel_rows):
    grouped = {}
    for row in panel_rows:
        key = (row["topic"], row["block"], row["world"], row["env"], row["arm"], row["agent"])
        grouped.setdefault(key, []).append(row)
    order = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
    series = []
    for key in sorted(grouped, key=lambda item: tuple(str(part) for part in item)):
        points = sorted(grouped[key], key=lambda row: order[row["tp"]])
        series.append({
            "topic": key[0], "block": key[1], "world": key[2], "env": key[3],
            "arm": key[4], "agent": key[5],
            "points": [{"tp": p["tp"], "sid": p["sid"], "BS": p["BS"], "Acc": p["Acc"],
                        "S": p["S"], "O": p["O"], "U": p["U"]} for p in points],
        })
    return series


def _case_from(row_from, row_to, label, rule):
    delta = row_to["BS"] - row_from["BS"]
    return {
        "label": label, "rule": rule,
        "key": {"topic": row_from["topic"], "block": row_from["block"],
                "world": row_from["world"], "env": row_from["env"],
                "arm": row_from["arm"], "agent": row_from["agent"]},
        "T1": {"sid": row_from["sid"], "BS": row_from["BS"], "Acc": row_from["Acc"],
               "S": row_from["S"], "O": row_from["O"], "U": row_from["U"]},
        "T3": {"sid": row_to["sid"], "BS": row_to["BS"], "Acc": row_to["Acc"],
               "S": row_to["S"], "O": row_to["O"], "U": row_to["U"]},
        "delta_BS": delta,
        "interpretation": ("BS 下降表示概率判断更接近金标准（越低越好）；本页仅作链路示例，"
                           "不构成认知改善结论。"),
    }


def _select_cases(panel_rows, enriched):
    """§12.3：固定 seed 下用确定性规则选改善/无变化/反向三类案例，不存在则如实标注。"""
    by_key = {}
    for row in panel_rows:
        if row["arm"] != "I4":
            continue
        by_key.setdefault((row["topic"], row["block"], row["world"], row["agent"]), {})[row["tp"]] = row
    candidates = []
    for key, points in by_key.items():
        if "T1" in points and "T3" in points:
            candidates.append((key, points["T1"], points["T3"]))
    candidates.sort(key=lambda item: tuple(str(part) for part in item[0]))
    improved = [c for c in candidates if c[2]["BS"] - c[1]["BS"] < -1e-12]
    reversed_ = [c for c in candidates if c[2]["BS"] - c[1]["BS"] > 1e-12]
    unchanged = sorted(candidates, key=lambda c: (abs(c[2]["BS"] - c[1]["BS"]),
                                                  tuple(str(p) for p in c[0])))
    notes = []
    result = {}
    used = set()
    if improved:
        key, one, two = min(improved, key=lambda c: (c[2]["BS"] - c[1]["BS"],
                                                     tuple(str(p) for p in c[0])))
        result["improved"] = _case_from(one, two, "改善示例", "固定 seed 下 I4 分支 BS 下降幅度最大")
        used.add(one["sid"])
    else:
        result["improved"] = None
        notes.append("不存在 BS 下降的改善案例（按固定 seed 如实标注，不编造）。")
    if reversed_:
        key, one, two = max(reversed_, key=lambda c: (c[2]["BS"] - c[1]["BS"],
                                                      tuple(str(p) for p in c[0])))
        result["reversed"] = _case_from(one, two, "反向变化示例", "固定 seed 下 I4 分支 BS 上升幅度最大")
        used.add(one["sid"])
    else:
        result["reversed"] = None
        notes.append("不存在 BS 上升的反向案例（按固定 seed 如实标注，不编造）。")
    if unchanged:
        key, one, two = next((c for c in unchanged if c[1]["sid"] not in used), unchanged[0])
        result["unchanged"] = _case_from(
            one, two, "无变化示例", "固定 seed 下 |ΔBS| 最小（若恰为 0 则为完全无变化）")
        if abs(two["BS"] - one["BS"]) > 1e-12:
            notes.append("无变化案例取 |ΔBS| 最小者，其 ΔBS 并非恰为 0，已如实标注。")
    else:
        result["unchanged"] = None
        notes.append("不存在无变化案例（按固定 seed 如实标注，不编造）。")
    # 为每个案例补 item_id / evidence_id 追溯终点
    for name in ("improved", "unchanged", "reversed"):
        case = result.get(name)
        if case is None:
            continue
        row = next(r for r in panel_rows if r["sid"] == case["T3"]["sid"])
        info = enriched[row["sid"]]
        gold = info["gold"]
        truths = {item["item_id"]: item["truth"] for item in gold["gold_answers"]
                  if item["partition"] == "anchor"}
        answers = [a for a in info["output"]["answers"] if a["item_id"] in truths]
        worst = max(answers, key=lambda a: (abs(a["p_true"] - truths[a["item_id"]]), a["item_id"]))
        cards = info["spec"]["visible_card_ids"]
        cited = worst["cited_card_ids"][0] if worst["cited_card_ids"] else (cards[0] if cards else None)
        case["trace_key"] = {
            "topic_id": row["topic"], "block_id": row["block"], "world_id": row["world"],
            "environment": row["env"], "arm": row["arm"], "agent_id": row["agent"],
            "item_id": worst["item_id"], "card_id": cited,
        }
        case["worst_item"] = {"item_id": worst["item_id"], "p_true": worst["p_true"],
                              "confidence": max(worst["p_true"], 1 - worst["p_true"]),
                              "correct": (worst["p_true"] >= 0.5) == bool(truths[worst["item_id"]])}
    return {"seed_rule": "固定预演 seed 下的确定性极值规则；案例只作解释，总体效应包含全部运行",
            "cases": result, "notes": notes}


def _calibration(panel_rows, enriched):
    """正误×信心分布；锚题8题与迁移题4题分母分离。"""
    bins = [0, 0.2, 0.4, 0.6, 0.8, 1.0000001]
    labels = ["0.5–0.6", "0.6–0.8", "0.8–1.0"]
    def empty():
        return {"anchor": {"correct": [0, 0, 0], "incorrect": [0, 0, 0]},
                "transfer": {"correct": [0, 0, 0], "incorrect": [0, 0, 0]},
                "anchor_n": 0, "transfer_n": 0}
    overall = empty()
    per_topic = {tid: empty() for tid in sr.TOPIC_ORDER}
    def bucket(confidence):
        if confidence < 0.6:
            return 0
        if confidence < 0.8:
            return 1
        return 2
    for row in panel_rows:
        info = enriched[row["sid"]]
        gold = info["gold"]
        truths = {item["item_id"]: item["truth"] for item in gold["gold_answers"]}
        partitions = {item["item_id"]: item["partition"] for item in gold["gold_answers"]}
        for target in (overall, per_topic[row["topic"]]):
            for answer in info["output"]["answers"]:
                item_id = answer["item_id"]
                if item_id not in truths:
                    continue
                probability = answer["p_true"]
                predicted = probability >= 0.5
                confidence = probability if predicted else 1 - probability
                correct = predicted == bool(truths[item_id])
                part = partitions[item_id]
                target[part]["correct" if correct else "incorrect"][bucket(confidence)] += 1
                target[part + "_n"] += 1
    def finish(node):
        return {
            "bin_labels": labels,
            "anchor": node["anchor"], "transfer": node["transfer"],
            "anchor_n": node["anchor_n"], "transfer_n": node["transfer_n"],
            "anchor_expected_n": 8, "transfer_expected_n": 4,
            "denominator_note": "锚题 8 题为主终点分母；迁移题 4 题仅探索性，二者不合并。",
        }
    return {"overall": finish(overall),
            "per_topic": {tid: finish(node) for tid, node in per_topic.items()}}


def _coverage(panel_rows):
    """六维覆盖（掌握/识别缺口/遗漏）与 U 三分项分布；按议题×时点聚合。"""
    per_topic = {}
    for topic_id in sr.TOPIC_ORDER:
        entry = {}
        for tp in ("T1", "T3"):
            subset = [r for r in panel_rows if r["topic"] == topic_id and r["tp"] == tp]
            if not subset:
                entry[tp] = None
                continue
            n = len(subset)
            distribution = {str(round(100 * value / 6, 4)): 0 for value in range(7)}
            for row in subset:
                key = str(round(100 * sum(row["u_parts"]) / 6, 4))
                distribution[key] = distribution.get(key, 0) + 1
            entry[tp] = {
                "n": n,
                "O_mean": sum(r["O"] for r in subset) / n,
                "U_mean": sum(r["U"] for r in subset) / n,
                "u_parts_mean": [sum(r["u_parts"][i] for r in subset) / n for i in range(3)],
                "mastered_rate": sum(r["mastered_rate"] for r in subset) / n,
                "gap_rate": sum(r["gap_rate"] for r in subset) / n,
                "missed_rate": sum(r["O"] for r in subset) / n,
                "U_distribution": distribution,
            }
        entry["dimension_order"] = ["掌握", "识别缺口", "遗漏"]
        entry["u_part_order"] = ["复述(summary)", "论据(main_evidence)", "边界(boundary)"]
        entry["note"] = "T3 为保持阶段主终点，T1 为干预前基线；O 的分母固定为 6 个私有维度。"
        per_topic[topic_id] = entry
    return per_topic


def _effects(rehearsal):
    exposure = rehearsal["analysis"]["exposure_2x2"]
    intervention = rehearsal["analysis"]["intervention"]
    def compact_exposure(node):
        return {
            "contract": node["contract"], "metric": node["metric"],
            "target_topics": node["target_topics"],
            "overall": {
                "estimable": node["overall"]["estimable"],
                "changes": node["overall"]["changes"],
                "contrasts": node["overall"]["contrasts"],
                "bounds_from_missingness": node["overall"]["bounds_from_missingness"],
            },
            "per_topic": {tid: {"estimable": row["estimable"],
                                "pooled_changes": row["pooled_changes"],
                                "contrasts": row["contrasts"],
                                "complete_blocks": row["complete_blocks"]}
                          for tid, row in node["per_topic"].items()},
            "per_block": [{"topic_id": b["topic_id"], "block_id": b["block_id"],
                           "valid": b["valid"], "changes": b["changes"],
                           "contrasts": b["contrasts"]} for b in node["per_block"]],
            "metric_direction": node["metric_direction"],
        }
    def compact_intervention(node):
        def one(item, secondary=False):
            return {
                "comparison_id": item.get("comparison_id"),
                "family": item.get("comparison_family"),
                "treatment_arm": item.get("treatment_arm"),
                "control_arm": item.get("control_arm"),
                "estimable": item.get("estimable"),
                "effect": item.get("effect"),
                "ci_95": item.get("ci_95"),
                "n": item.get("n"), "expected_n": item.get("expected_n"),
                "coverage": item.get("coverage"),
                "control": item.get("control"), "treatment": item.get("treatment"),
                "p_raw": item.get("p_raw"), "p_adjusted": item.get("p_adjusted"),
                "p_value_status": item.get("p_value_status"),
                "secondary": secondary,
            }
        return {
            "endpoint_timepoint": node["endpoint_timepoint"],
            "metric": node["metric"], "metric_scale": node["metric_scale"],
            "effect_definition": node["effect_definition"],
            "comparisons": {name: one(item) for name, item in node["comparisons"].items()},
            "secondary_comparisons": {name: one(item, True)
                                      for name, item in node["secondary_comparisons"].items()},
            "p_raw": node["p_raw"], "p_adjusted": node["p_adjusted"],
            "randomization": node["randomization"],
            "allocation_validation": node["allocation_validation"],
            "p_value_note": node["p_value_note"],
            "randomization_scope": node["randomization_scope"],
            "coverage": node["coverage"],
        }
    return {
        "exposure": compact_exposure(exposure),
        "intervention": {tp: compact_intervention(node) for tp, node in intervention.items()},
        "note": ("全部效应与 p 值均由确定性合成回答产生，仅验证统计链路，不是实验结论。"),
    }


def _network_view(bundle, populations, networks):
    rows = []
    for (topic_id, block_id), population in sorted(populations.items()):
        edges = networks[(topic_id, block_id)]
        count = len(population)
        nodes = []
        for index, agent in enumerate(population):
            angle = 2 * math.pi * index / count
            nodes.append({
                "agent_id": agent["agent_id"], "stance": agent["initial_stance"],
                "x": round(140 + 100 * math.cos(angle), 3),
                "y": round(140 + 100 * math.sin(angle), 3),
            })
        by_id = {node["agent_id"]: node for node in nodes}
        edge_rows = []
        for edge in edges:
            kind = "same" if by_id[edge["follower_id"]]["stance"] == by_id[edge["followee_id"]]["stance"] else "opposite"
            edge_rows.append({"source": edge["follower_id"], "target": edge["followee_id"], "kind": kind})
        rows.append({
            "topic": topic_id, "block": block_id,
            "base_population_network_sha256": _sha({"agents": population, "network": edges}),
            "nodes": nodes, "edges": edge_rows,
            "same_stance_edges": sum(1 for e in edge_rows if e["kind"] == "same"),
            "opposite_stance_edges": sum(1 for e in edge_rows if e["kind"] == "opposite"),
            "note": "固定坐标；严格受控层中新回复不实时传给其他受试，边只表示关注关系。",
        })
    return rows


def _interventions(bundle):
    rows = []
    for topic_id in sr.TOPIC_ORDER:
        zones = bundle["topics"][topic_id]["zones"]
        public = zones["zone_1_subject_public"]
        packets = zones["zone_2_assistant_evidence"]["packets"]
        for arm in sr.ARMS:
            for round_no in sr.ROUNDS:
                item = public["intervention_packets"][arm][str(round_no)]
                packet = packets[f"round_{round_no}"]
                rows.append({
                    "topic": topic_id, "arm": arm, "round": round_no,
                    "char_count": item["char_count"],
                    "visible_text": item["visible_text"],
                    "visible_text_sha256": _bytes_sha(item["visible_text"]),
                    "additional_chars": item["additional_chars"],
                    "evidence_ids": list(packet["evidence_ids"]),
                    "packet_id": item.get("packet_path"),
                    "evidence_block_sha256": item["evidence_block_sha256"],
                    "source_map_sha256": item["source_map_sha256"],
                    "response_prompt": item["response_prompt"],
                    "response_opportunities": item["response_opportunities"],
                    "retention_cards_used": item["retention_cards_used"],
                    "contains_treatment_label": item["contains_treatment_label"],
                    "extractive_constraint_satisfied":
                        item.get("extractive_control_constraint_satisfied") is True,
                    "content_origin": item.get("content_origin"),
                    "live_provenance": item.get("live_provenance"),
                })
    return rows


def _evidence(bundle):
    sources, items, cards, warnings = [], [], [], []
    for topic_id in sr.TOPIC_ORDER:
        topic = bundle["topics"][topic_id]
        zones = topic["zones"]
        public = zones["zone_1_subject_public"]
        for binding in topic["source_bindings"]:
            sources.append({
                "topic": topic_id, "source_id": binding["source_id"], "url": binding["url"],
                "title": binding["title"], "publication_date": binding["publication_date"],
                "source_level": binding["source_level"],
                "source_family_id": binding["source_family_id"],
                "text_sha256": binding["text_sha256"], "raw_sha256": binding["raw_sha256"],
                "redistribution_cleared": binding["redistribution_cleared"],
            })
        for row in topic["topic_evidence"]:
            items.append({
                "topic": topic_id, "evidence_id": row["evidence_id"],
                "source_id": row["source_id"], "content_sha256": row["content_sha256"],
                "quote_start": row["quote_start"], "quote_end": row["quote_end"],
                "transformation_type": row["transformation_type"],
                "boundary_note": row["boundary_note"],
            })
        for phase in ("exposure", "retention"):
            for card in public[f"public_{phase}_cards"]:
                cards.append({
                    "topic": topic_id, "card_id": card["card_id"],
                    "evidence_id": card.get("evidence_id"),
                    "source_id": card.get("source_id"),
                    "phase": phase, "stance": card.get("stance"),
                    "is_accurate": card.get("is_accurate"),
                    "text_sha256": _bytes_sha(card["text"]),
                    "source_family_id": card["source_family_id"],
                    "transformation_type": card["transformation_type"],
                })
        for row in topic["rights_warnings"]:
            warnings.append({"topic": topic_id, **deepcopy(row)})
    return {"sources": sources, "items": items, "cards": cards, "rights_warnings": warnings}


def _timeline(rehearsal):
    worlds = rehearsal["stage2_exposure"]["worlds"]
    branches = rehearsal["stage3_branches"]["branches"]
    rows = []
    for topic_id in sr.TOPIC_ORDER:
        for block in ("B01", "B02")[:rehearsal["parameters"]["blocks_per_topic"]]:
            group = [w for w in worlds if w["topic_id"] == topic_id and w["block_id"] == block]
            branch_group = [b for b in branches if b["topic_id"] == topic_id and b["block_id"] == block]
            steps = [{"label": "T0", "logical_seq": 0, "phase": "pre",
                      "sha256": group[0]["initial_state_core_sha256"] if group else None,
                      "note": "接触材料前；可见卡为空"}]
            for round_no in range(1, sr.EXPOSURE_ROUNDS + 1):
                steps.append({"label": f"曝光R{round_no}", "logical_seq": round_no * 100,
                              "phase": "exposure", "sha256": None,
                              "note": "6 张公共卡 + 1 个冻结讨论槽"})
            steps.append({"label": "T1 快照", "logical_seq": 900, "phase": "snapshot",
                          "sha256": group[3]["t1_snapshot_sha256"] if len(group) > 3 else None,
                          "note": "第8轮末；六臂共享同一父快照"})
            for index, env in enumerate(sr.ENVIRONMENTS):
                steps.append({"label": f"T1-{env}", "logical_seq": 900 + index,
                              "phase": "snapshot",
                              "sha256": group[index]["t1_snapshot_sha256"] if len(group) > index else None,
                              "note": f"{env} 环境 T1 快照哈希"})
            steps.append({"label": "六臂分支", "logical_seq": 1000, "phase": "branch",
                          "sha256": branch_group[0]["parent_t1_sha256"] if branch_group else None,
                          "note": "I0–I5 整网克隆，处理前核心哈希一致"})
            steps.append({"label": "干预R1", "logical_seq": 1100, "phase": "intervention",
                          "sha256": branch_group[0]["branch_pre_treatment_core_sha256"] if branch_group else None,
                          "note": "六臂各 600–800 字符可见文本"})
            steps.append({"label": "干预R2", "logical_seq": 1200, "phase": "intervention",
                          "sha256": None, "note": "每轮一次回应机会"})
            steps.append({"label": "T2", "logical_seq": 1300, "phase": "measurement",
                          "sha256": None, "note": "8 锚题 + 4 迁移题"})
            for round_no in range(1, sr.RETENTION_ROUNDS + 1):
                steps.append({"label": f"保持R{round_no}", "logical_seq": 1300 + round_no * 100,
                              "phase": "retention", "sha256": None,
                              "note": "独立保持卡池，不占用干预库存"})
            steps.append({"label": "T3 主终点", "logical_seq": 1800, "phase": "measurement",
                          "sha256": None, "note": "BS 主要终点"})
            rows.append({"topic": topic_id, "block": block, "steps": steps})
    return rows


def _info_flow(rehearsal, bundle):
    summary = rehearsal["stage2_exposure"]["manipulation_summary"]
    rows = []
    for env in sr.ENVIRONMENTS:
        row = summary[env]
        family_counts, transform_counts = {}, {}
        for topic_id in sr.TOPIC_ORDER:
            for card in bundle["topics"][topic_id]["zones"]["zone_1_subject_public"]["public_exposure_cards"]:
                family_counts[card["source_family_id"]] = family_counts.get(card["source_family_id"], 0) + 1
                transform_counts[card["transformation_type"]] = transform_counts.get(card["transformation_type"], 0) + 1
        rows.append({
            "environment": env,
            "X": row["mean_X"], "Hn": row["mean_Hn"],
            "error_ratio": row["mean_error_ratio"], "max_repeat": row["max_repeat"],
            "quota": ({"same": 2, "opposite": 2, "neutral": 2} if env in ("A", "B")
                      else {"same": 4, "opposite": 1, "neutral": 1}),
            "cards_per_round": 6, "rounds": sr.EXPOSURE_ROUNDS,
            "source_family_counts": family_counts,
            "transformation_types": transform_counts,
            "note": "错误卡按预生成联合分层表分配；受试看不到真假标签。",
        })
    return rows


def _governance(gate, rehearsal, merge):
    checklist = gate["checklist"]
    disputes = []
    for row in merge["disagreement_items"]:
        reason = next(
            item["adjudication"]["reason"]
            for group in hr._GROUPS
            for item in merge["merged_document"][group]
            if item["id"] == row["item_id"])
        # 展示层兜底：来源发布日期缺失时，裁决理由里的 “None” 改为“日期未明”。
        reason = reason.replace("，None）", "，日期未明）").replace("（None）", "（日期未明）")
        disputes.append({
            "item_id": row["item_id"], "group": row["group"], "check": row["check"],
            "reviewer_1_decision": row["reviewer_1_decision"],
            "reviewer_2_decision": row["reviewer_2_decision"],
            "reason": row["reason"],
            "final_decision": merge["merged_document"]["item_results"][row["item_id"]]["final_decision"],
            "resolution_basis": merge["merged_document"]["item_results"][row["item_id"]]["resolution_basis"],
            "adjudication_reason": reason,
        })
    mirror = rehearsal["stage2_exposure"]["mirror_checks"]
    citation = {
        "mirror_checks": len(mirror),
        "schema_valid": sum(1 for c in mirror if c["citation_audit"]["schema_valid"] is True),
        "known_evidence_only": sum(1 for c in mirror if c["citation_audit"]["known_evidence_only"] is True),
        "pass_rate": (sum(1 for c in mirror if c["citation_audit"]["schema_valid"] is True)
                      / len(mirror)) if mirror else None,
        "semantic_entailment": "pending_independent_review",
    }
    return {
        "checklist": checklist,
        "disputes": disputes,
        "acceptance_gates": rehearsal["acceptance_gates"],
        "citation": citation,
        "review_origin": gate["review_origin"],
        "real_human_ca_signature_verified": gate["real_human_ca_signature_verified"],
        "formal_eligible": False,
        "approved_for_experiment": False,
    }


def _indexes(evidence):
    sources = {(row["topic"], row["source_id"]): row for row in evidence["sources"]}
    items = {(row["topic"], row["evidence_id"]): row for row in evidence["items"]}
    cards = {(row["topic"], row["card_id"]): row for row in evidence["cards"]}
    return sources, items, cards


def _worst_item(enriched_row):
    gold = enriched_row["gold"]
    truths = {item["item_id"]: item["truth"] for item in gold["gold_answers"]
              if item["partition"] == "anchor"}
    answers = [a for a in enriched_row["output"]["answers"] if a["item_id"] in truths]
    if not answers:
        return None, truths
    worst = min(answers, key=lambda a: (-abs(a["p_true"] - truths[a["item_id"]]), a["item_id"]))
    return worst, truths


def _chain(row, enriched, indexes, network_index, effects, *, kind):
    sources, items, cards = indexes
    info = enriched[row["sid"]]
    spec = info["spec"]
    visible = list(spec["visible_card_ids"])
    answer, truths = _worst_item(info)
    card_id = (answer["cited_card_ids"][0] if answer and answer["cited_card_ids"]
               else (visible[0] if visible else None))
    card = cards.get((row["topic"], card_id)) if card_id else None
    evidence_row = items.get((row["topic"], card["evidence_id"])) if card else None
    source_row = sources.get((row["topic"], evidence_row["source_id"])) if evidence_row else None
    nodes = []
    if source_row:
        nodes.append({"type": "source", "id": source_row["source_id"],
                      "value": {"url": source_row["url"],
                                "text_sha256": source_row["text_sha256"],
                                "raw_sha256": source_row["raw_sha256"]}})
    if evidence_row:
        nodes.append({"type": "evidence", "id": evidence_row["evidence_id"],
                      "value": {"source_id": evidence_row["source_id"],
                                "content_sha256": evidence_row["content_sha256"],
                                "quote_start": evidence_row["quote_start"],
                                "quote_end": evidence_row["quote_end"]}})
    if card:
        nodes.append({"type": "card", "id": card["card_id"],
                      "value": {"evidence_id": card["evidence_id"],
                                "source_id": card["source_id"],
                                "text_sha256": card["text_sha256"],
                                "phase": card["phase"]}})
    nodes.append({"type": "visible", "id": row["agent"], "value": {
        "world_id": row["world"], "count": len(visible),
        "card_ids": visible, "sha256": _sha(visible)}})
    nodes.append({"type": "answer", "id": row["sid"], "value": {
        "item_id": answer["item_id"] if answer else None,
        "p_true": answer["p_true"] if answer else None,
        "cited_card_ids": answer["cited_card_ids"] if answer else [],
        "output_sha256": row["msha"],
        "correct": (bool((answer["p_true"] >= 0.5) == truths[answer["item_id"]]))
                   if answer else None}})
    nodes.append({"type": "blind", "id": row["sid"], "value": {
        "u_parts": row["u_parts"], "U": row["U"], "O": row["O"],
        "mastered_rate": row["mastered_rate"], "gap_rate": row["gap_rate"]}})
    network = network_index.get((row["topic"], row["block"], row["world"], row["arm"], row["tp"]))
    if network:
        nodes.append({"type": "network", "id": network["world_id"], "value": {
            "timepoint": network["timepoint"], "BS": network["BS"], "O": network["O"],
            "U": network["U"], "valid": network["valid"],
            "source_blind_sample_ids": network["source_blind_sample_ids"]}})
    if row["tp"] in ("T2", "T3") and row["arm"] in ("I0", "I4", "I5"):
        comparison = effects["intervention"][row["tp"]]["comparisons"].get("I4_vs_I0")
        if comparison:
            nodes.append({"type": "effect", "id": f"I4_vs_I0@{row['tp']}", "value": {
                "effect": comparison["effect"], "ci_95": comparison["ci_95"],
                "p_raw": comparison["p_raw"], "p_adjusted": comparison["p_adjusted"]}})
    return {"path_id": None, "kind": kind,
            "key": {"topic_id": row["topic"], "block_id": row["block"],
                    "world_id": row["world"], "environment": row["env"],
                    "arm": row["arm"], "agent_id": row["agent"],
                    "timepoint": row["tp"], "item_id": answer["item_id"] if answer else None,
                    "evidence_id": evidence_row["evidence_id"] if evidence_row else None,
                    "condition_tag": spec["condition_tag"]},
            "nodes": nodes}


def _build_trace_paths(panel_rows, enriched, evidence, effects, network_records, cases,
                       info_flow):
    indexes = _indexes(evidence)
    network_index = {(r["topic_id"], r["block_id"], r["world_id"], r["arm"], r["timepoint"]): r
                     for r in network_records}
    paths = []
    for name in ("improved", "unchanged", "reversed"):
        case = cases["cases"].get(name)
        if case is None:
            continue
        for tp_key, tp in (("T1", "T1"), ("T3", "T3")):
            sid = case[tp_key]["sid"]
            row = next(r for r in panel_rows if r["sid"] == sid)
            paths.append(_chain(row, enriched, indexes, network_index, effects,
                                kind=f"case_{name}_{tp}"))
    step = max(1, len(panel_rows) // 9)
    for index in range(0, len(panel_rows), step):
        if len([p for p in paths if p["kind"] == "sampled"]) >= 9:
            break
        paths.append(_chain(panel_rows[index], enriched, indexes, network_index, effects,
                            kind="sampled"))
    for topic_id in sr.TOPIC_ORDER:
        sample_item = next((row for row in evidence["items"] if row["topic"] == topic_id), None)
        if sample_item is None:
            continue
        source_row = indexes[0][(topic_id, sample_item["source_id"])]
        cards = [c for c in evidence["cards"] if c["topic"] == topic_id
                 and c["evidence_id"] == sample_item["evidence_id"]]
        nodes = [
            {"type": "source", "id": source_row["source_id"],
             "value": {"url": source_row["url"], "text_sha256": source_row["text_sha256"],
                       "raw_sha256": source_row["raw_sha256"]}},
            {"type": "evidence", "id": sample_item["evidence_id"],
             "value": {"source_id": sample_item["source_id"],
                       "content_sha256": sample_item["content_sha256"],
                       "quote_start": sample_item["quote_start"],
                       "quote_end": sample_item["quote_end"]}},
        ]
        if cards:
            nodes.append({"type": "card", "id": cards[0]["card_id"],
                          "value": {"evidence_id": cards[0]["evidence_id"],
                                    "source_id": cards[0]["source_id"],
                                    "text_sha256": cards[0]["text_sha256"],
                                    "phase": cards[0]["phase"]}})
        paths.append({"path_id": None, "kind": "source_to_card",
                      "key": {"topic_id": topic_id, "evidence_id": sample_item["evidence_id"]},
                      "nodes": nodes})
    for topic_id in sr.TOPIC_ORDER:
        contrast = effects["exposure"]["per_topic"].get(topic_id, {})
        rows = [r for r in panel_rows if r["topic"] == topic_id and r["arm"] == "I4"
                and r["tp"] == "T3"]
        if not rows:
            continue
        row = rows[0]
        chain = _chain(row, enriched, indexes, network_index, effects, kind="effect")
        chain["nodes"].append({"type": "aggregate", "id": f"2x2@{topic_id}", "value": {
            "contrasts": contrast.get("contrasts"),
            "pooled_changes": contrast.get("pooled_changes")}})
        paths.append(chain)
    for environment in sr.ENVIRONMENTS:
        rows = [r for r in panel_rows if r["env"] == environment and r["arm"] is None
                and r["tp"] == "T1"]
        if not rows:
            continue
        chain = _chain(rows[0], enriched, indexes, network_index, effects,
                       kind=f"info_flow_{environment}")
        flow = next((row for row in info_flow if row["environment"] == environment), {})
        chain["nodes"].append({"type": "aggregate", "id": f"env@{environment}", "value": {
            "X": flow.get("X"), "Hn": flow.get("Hn"),
            "error_ratio": flow.get("error_ratio")}})
        paths.append(chain)
    for index, path in enumerate(paths, 1):
        path["path_id"] = f"TP-{index:03d}"
    return paths


def build_dashboard_dataset(evidence_root, *, allow_ai_assisted_release=True,
                            seed=20261109) -> dict:
    """构建可复核派生数据包；所有数值来自既有模块输出，不新造结论。"""
    _need(type(allow_ai_assisted_release) is bool, "release_flag_invalid")
    _need(type(seed) is int, "seed_invalid")
    build = _resolve_build(evidence_root)
    bundle = pm.build_all_topics_pilot_materials(evidence_root)
    merge = aar.run_scheme_a_merge_and_verify(
        aar.DEFAULT_TEMPLATE_PATH, evidence_build=build,
        live_semantic_path=aar.DEFAULT_LIVE_SEMANTIC_PATH)
    gate = aar.evaluate_ai_assisted_release_gate(
        evidence_root, allow_ai_assisted_release=allow_ai_assisted_release)
    rehearsal = sr.run_pilot_system_rehearsal(evidence_root, seed=seed)
    rehearsal_verification = sr.verify_pilot_system_rehearsal(rehearsal)
    _need(rehearsal_verification["passed"] is True, "rehearsal_verification_failed")

    parameters = rehearsal["parameters"]
    samples, sid_to_index, enriched, populations, networks = _rebuild_samples(
        bundle, parameters, rehearsal)
    panel = rehearsal["analysis"]["reconciled_panel"]["panel_records"]
    panel_rows = _panel_rows(panel, enriched)
    series = _series(panel_rows)
    cases = _select_cases(panel_rows, enriched)
    effects = _effects(rehearsal)
    evidence = _evidence(bundle)
    network_view = _network_view(bundle, populations, networks)
    live = lcd.summarize_live_delivery(Path(__file__).resolve().parents[1])
    micro = lmp.summarize_live_micro_pilot(Path(__file__).resolve().parents[1])
    micro_matrix = lmp.summarize_three_topic_matrix(Path(__file__).resolve().parents[1])
    closed_world = cwp.summarize_closed_world_live_pilot(Path(__file__).resolve().parents[1])
    dual_track = _dual_track(micro_matrix, closed_world)
    dose_collisions = [
        {"topic": topic_id, "round": row["round"], "arm": row["arm"],
         "additional_chars": row["additional_chars"]}
        for topic_id in sr.TOPIC_ORDER
        for row in bundle["topics"][topic_id]["zones"]["zone_1_subject_public"]
        .get("dose_arm_collisions", [])]
    interventions = _interventions(bundle)
    calibration = _calibration(panel_rows, enriched)
    coverage = _coverage(panel_rows)
    info_flow = _info_flow(rehearsal, bundle)
    governance = _governance(gate, rehearsal, merge)
    billing = _billing_summary(DEFAULT_LEDGER)
    milestones = _validation_milestones(DEFAULT_VALIDATION_DIR)
    failures = _delivery_failures(DEFAULT_DELIVERY_DIR)
    trace_paths = _build_trace_paths(panel_rows, enriched, evidence, effects,
                                     rehearsal["analysis"]["network_aggregation"]["network_records"],
                                     cases, info_flow)

    matrix = []
    for topic_id in sr.TOPIC_ORDER:
        for block in ("B01", "B02")[:parameters["blocks_per_topic"]]:
            matrix.append({
                "topic_id": topic_id, "block_id": block,
                "worlds": len(sr.ENVIRONMENTS),
                "branches": len(sr.ARMS),
                "panel_samples": sum(1 for row in panel_rows
                                     if row["topic"] == topic_id and row["block"] == block),
                "network_records": sum(
                    1 for row in rehearsal["analysis"]["network_aggregation"]["network_records"]
                    if row["topic_id"] == topic_id and row["block_id"] == block),
                "environments": list(sr.ENVIRONMENTS),
            })
    topic_cards = {tid: {"exposure": len(bundle["topics"][tid]["zones"]["zone_1_subject_public"]["exposure_cards"]),
                         "retention": len(bundle["topics"][tid]["zones"]["zone_1_subject_public"]["retention_cards"])}
                   for tid in sr.TOPIC_ORDER}
    dataset = {
        "contract": DATASET_VERSION,
        "generated_at_utc": _utc_now(),
        "banner": {"text": BANNER_TEXT, "persistent": True, "position": "top",
                   "is_demo": True, "actual_model_calls": 0, "formal_eligible": False},
        "protocol": {"path": PROTOCOL_PATH.name,
                     "sha256": _file_sha(PROTOCOL_PATH) if PROTOCOL_PATH.is_file() else None,
                     "sections": ["12.1", "12.2", "12.3", "12.4", "16"]},
        "honest_gates": {
            "rehearsal_mode": sr.REHEARSAL_MODE, "is_demo": True,
            "actual_model_calls": 0, "actual_api_calls": 0, "new_cost_cny": "0",
            "human_reviews_completed": 0, "approved_for_experiment": False,
            "formal_eligible": False, "formal_launch_enabled": False,
            "statistical_outputs_are_synthetic": True,
            "review_origin": aar.REVIEW_ORIGIN,
            "real_human_ca_signature_verified": False,
        },
        "parameters": {
            "seed": seed, "blocks_per_topic": parameters["blocks_per_topic"],
            "agents_per_world": parameters["agents_per_world"],
            "topics": list(sr.TOPIC_ORDER), "environments": list(sr.ENVIRONMENTS),
            "arms": list(sr.ARMS), "timepoints": ["T0", "T1", "T2", "T3"],
            "exposure_rounds": sr.EXPOSURE_ROUNDS,
            "intervention_rounds": len(sr.ROUNDS),
            "retention_rounds": sr.RETENTION_ROUNDS,
            "allow_ai_assisted_release": allow_ai_assisted_release,
            "bootstrap_reps": parameters["bootstrap_reps"],
            "permutation_reps": parameters["permutation_reps"],
        },
        "overview": {
            "matrix": matrix,
            "topic_cards": topic_cards,
            "worlds": rehearsal["stage2_exposure"]["world_count"],
            "branches": rehearsal["stage3_branches"]["branch_count"],
            "panel_samples": len(panel_rows),
            "network_records": len(rehearsal["analysis"]["network_aggregation"]["network_records"]),
            "missing_agents_total": rehearsal["stage4_measurement"]["missing_agents_total"],
            "checklist": {
                "review_origin": gate["review_origin"],
                "human_slots_completed": gate["checklist"]["human_slots_completed"],
                "items_total": gate["checklist"]["items_total"],
                "items_passed": gate["checklist"]["items_passed"],
                "disagreement_count": gate["checklist"]["disagreement_count"],
                "unresolved_disagreements": gate["checklist"]["unresolved_disagreements"],
                "human_reviews_completed": gate["checklist"]["human_reviews_completed"],
                "content_review_passed": gate["checklist"]["content_review_passed"],
                "release_gates_passed": gate["checklist"]["release_gates_passed"],
                "all_55_items_resolved": gate["checklist"]["all_55_items_resolved"],
                "all_55_items_passed": gate["checklist"]["all_55_items_passed"],
                "pilot_release_unlocked": gate["pilot_release_unlocked"],
                "release_scope": gate["release_scope"],
                "role_ids": gate["checklist"]["role_ids"],
            },
            "billing": billing,
            "failures": failures,
            "milestones": milestones,
            "rehearsal_verification": {
                "passed": rehearsal_verification["passed"],
                "checks": rehearsal_verification["checks"],
                "samples_reconciled": rehearsal_verification["samples_reconciled"],
            },
        },
        "live_delivery": live,
        "live_micro_pilot": micro,
        "live_micro_matrix": micro_matrix,
        "closed_world_pilot": closed_world,
        "dual_track": dual_track,
        "dose_collisions": dose_collisions,
        "timeline": _timeline(rehearsal),
        "network": network_view,
        "info_flow": info_flow,
        "panel": panel_rows,
        "network_records": [{
            "topic_id": row["topic_id"], "block_id": row["block_id"],
            "world_id": row["world_id"], "environment": row["environment"],
            "arm": row["arm"], "timepoint": row["timepoint"],
            "observed_agents": row["observed_agents"], "missing_agents": row["missing_agents"],
            "valid": row["valid"], "blind_valid": row["blind_valid"],
            "BS": row["BS"], "Acc": row["Acc"], "S": row["S"], "O": row["O"], "U": row["U"],
            "BS_lower_mean": row["BS_lower_mean"], "BS_upper_mean": row["BS_upper_mean"],
            "source_blind_sample_ids": list(row["source_blind_sample_ids"]),
        } for row in rehearsal["analysis"]["network_aggregation"]["network_records"]],
        "series": series,
        "cases": cases,
        "interventions": interventions,
        "evidence": evidence,
        "effects": effects,
        "calibration": calibration,
        "coverage": coverage,
        "governance": governance,
        "trace_paths": trace_paths,
        "data_lineage": {
            "panel_source": "system_rehearsal.analysis.reconciled_panel.panel_records",
            "network_source": "system_rehearsal.analysis.network_aggregation.network_records",
            "effects_source": "system_rehearsal.analysis.exposure_2x2 / intervention",
            "checklist_source": "ai_assisted_review.evaluate_ai_assisted_release_gate",
            "materials_source": "pilot_materials.build_all_topics_pilot_materials",
            "reconstruction_note": (
                "逐样本 visible_card_ids 由 scheduling.schedule_cards 按固定 seed 重建，"
                "并与 reconcile 面板的 measurement_output_sha256 与 U 三分项逐条对账。"),
            "checks": {
                "samples": len(samples), "panel_rows": len(panel_rows),
                "sid_permutation_matches": True,
                "output_hash_matches": len(panel_rows),
                "u_parts_matches": len(panel_rows),
            },
        },
        "lineage_paths": {
            "evidence_root": str(Path(evidence_root).resolve()),
            "evidence_build_dir": str(build),
            "template_path": str(aar.DEFAULT_TEMPLATE_PATH),
            "live_semantic_path": str(aar.DEFAULT_LIVE_SEMANTIC_PATH),
            "ledger_path": str(DEFAULT_LEDGER),
        },
        "questionnaires": {tid: bundle["topics"][tid]["zones"]["zone_3_evaluator_private_gold"]
                           ["public_questionnaire"] for tid in sr.TOPIC_ORDER},
        "views": [{"id": view[0], "title": view[1], "description": view[2]} for view in VIEWS],
        "notice": ("只读派生看板；原始日志不可在页面修改。案例只作解释，总体效应包含全部运行；"
                   "所有合成标记与失败记录默认可见。"),
    }
    dataset["spotlight_tour"] = _spotlight_tour(dataset["overview"], len(trace_paths), micro_matrix,
                                                closed_world, dual_track, billing)
    dataset["dataset_sha256"] = _sha({k: v for k, v in dataset.items() if k != "dataset_sha256"})
    return dataset


# --------------------------------------------------------------- 静态渲染
def _esc(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _n(value, digits=4):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _esc(value)
    if number != number:
        return "—"
    if abs(number) >= 1000:
        return f"{number:,.1f}"
    return f"{number:.{digits}f}".rstrip("0").rstrip(".") or "0"


def _table(headers, rows, cls="tbl", caption=None):
    out = [f'<div class="table-wrap"><table class="{cls}">']
    if caption:
        out.append(f"<caption>{_esc(caption)}</caption>")
    out.append("<thead><tr>" + "".join(f"<th>{_esc(h)}</th>" for h in headers) + "</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _kpi(items):
    return '<div class="kpi">' + "".join(
        f'<div class="kpi-item"><div class="kpi-label">{_esc(label)}</div>'
        f'<div class="kpi-value">{_esc(value)}</div></div>' for label, value in items) + "</div>"


def _svg_network(node):
    parts = ['<svg class="net" viewBox="0 0 280 280" role="img" aria-label="8人基础网络拓扑">']
    for edge in node["edges"]:
        a = next(n for n in node["nodes"] if n["agent_id"] == edge["source"])
        b = next(n for n in node["nodes"] if n["agent_id"] == edge["target"])
        color = "#2563eb" if edge["kind"] == "same" else "#d97706"
        dash = "" if edge["kind"] == "same" else ' stroke-dasharray="4 3"'
        parts.append(f'<line x1="{a["x"]}" y1="{a["y"]}" x2="{b["x"]}" y2="{b["y"]}" '
                     f'stroke="{color}" stroke-opacity="0.45" stroke-width="1.1"{dash}/>')
    for item in node["nodes"]:
        fill = "#2563eb" if item["stance"] == 1 else "#d97706"
        parts.append(f'<circle cx="{item["x"]}" cy="{item["y"]}" r="13" fill="{fill}" '
                     f'fill-opacity="0.9" stroke="#ffffff" stroke-width="2"/>')
        parts.append(f'<text x="{item["x"]}" y="{item["y"] + 4}" text-anchor="middle" '
                     f'font-size="9" fill="#ffffff">{_esc(item["agent_id"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_forest(exposure, intervention):
    rows = []
    for endpoint in ("T2", "T3"):
        for name, item in intervention[endpoint]["comparisons"].items():
            if item["effect"] is None:
                continue
            rows.append((f"{name}@{endpoint}", item["effect"], item["ci_95"],
                         item["p_raw"], item["p_adjusted"]))
    for name in ("I1_vs_I0", "I2_vs_I0", "I3_vs_I0", "I5_vs_I0"):
        item = intervention["T3"]["secondary_comparisons"].get(name)
        if item and item["effect"] is not None:
            rows.append((f"{name}@T3", item["effect"], item["ci_95"],
                         item["p_raw"], item["p_adjusted"]))
    values = [item[1] for item in rows] + [v for item in rows for v in (item[2] or [])]
    if not values:
        return "<p>无可估计的干预效应。</p>"
    bound = max(0.001, max(abs(v) for v in values) * 1.15)
    width, left, top, row_height = 520, 210, 26, 26
    height = top + row_height * max(1, len(rows)) + 24
    def x(value):
        return left + (value + bound) / (2 * bound) * (width - left - 40)
    parts = [f'<svg class="forest" viewBox="0 0 {width} {height}" role="img" aria-label="配对效应森林图">',
             f'<line x1="{x(0)}" y1="{top - 12}" x2="{x(0)}" y2="{height - 18}" stroke="#94a3b8" stroke-dasharray="3 3"/>',
             f'<text x="{x(0)}" y="{height - 4}" font-size="10" text-anchor="middle" fill="#475569">0</text>']
    for index, (label, effect, ci, p_raw, p_adj) in enumerate(rows):
        y = top + index * row_height
        parts.append(f'<text x="6" y="{y + 4}" font-size="10" fill="#0f172a">{_esc(label)}</text>')
        if ci:
            parts.append(f'<line x1="{x(ci[0])}" y1="{y}" x2="{x(ci[1])}" y2="{y}" stroke="#334155" stroke-width="1.4"/>')
            parts.append(f'<line x1="{x(ci[0])}" y1="{y - 4}" x2="{x(ci[0])}" y2="{y + 4}" stroke="#334155"/>')
            parts.append(f'<line x1="{x(ci[1])}" y1="{y - 4}" x2="{x(ci[1])}" y2="{y + 4}" stroke="#334155"/>')
        color = "#1d4ed8" if p_adj is not None and p_adj < 0.05 else "#64748b"
        parts.append(f'<circle cx="{x(effect)}" cy="{y}" r="4.5" fill="{color}"/>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_bars(rows, max_value=None):
    if not rows:
        return ""
    top = max_value or max(max(r[1], 0.0001) for r in rows)
    width, height, left = 460, 20 + 24 * len(rows), 130
    parts = [f'<svg class="bars" viewBox="0 0 {width} {height}" role="img" aria-label="条形图">']
    for index, (label, value, color) in enumerate(rows):
        y = 8 + index * 24
        width_bar = max(1, (value / top) * (width - left - 40))
        parts.append(f'<text x="4" y="{y + 13}" font-size="10" fill="#0f172a">{_esc(label)}</text>')
        parts.append(f'<rect x="{left}" y="{y + 3}" width="{width_bar:.1f}" height="14" fill="{color}" fill-opacity="0.85"/>')
        parts.append(f'<text x="{left + width_bar + 4}" y="{y + 14}" font-size="9" fill="#475569">{_n(value, 2)}</text>')
    parts.append("</svg>")
    return "".join(parts)



def _live_delivery_summary(live):
    """view-01：六轮真实组织侧＋原句摘录配对的总体状态。"""
    if not live.get("available"):
        return ('<p class="note warn">六轮真实 mimo 组织侧尚未完成或未通过核验：'
                f'{_esc(live.get("reason", "unknown"))}；看板只展示已发生的事实。</p>')
    rows = [[_esc(row["case_id"]), _esc(row["run_id"]),
             "复用" if row["reused"] else "本轮真实调用",
             row["attempts"], row["organized_chars"], row["plain_chars"],
             f'{row["visible_chars"]["I4"]} / {row["visible_chars"]["I5"]}',
             "通过" if all(row["checks"].values()) else "未通过",
             _esc(row["charged_cny"])] for row in live["cases"]]
    return (
        _kpi([("真实组织侧轮次", f'{live["case_count"]}/6'),
              ("本轮新增真实调用", live["new_live_calls"]),
              ("六轮账本合计 (¥)", live["charged_cny"]),
              ("核验状态", "通过" if live["available"] else "未通过")])
        + _table(["轮次", "Run ID", "来源", "尝试", "I4 字符", "I5 字符",
                  "可见总长 I4/I5", "七项配对检查", "账本费用(¥)"], rows,
                 caption="步骤39：六轮真实 mimo-v2.6-flash 组织侧＋确定性原句摘录配对")
        + f'<p class="note">清单 SHA-256 <code>{_esc(live["manifest_sha256"])}</code>；'
          f'Token 合计 {live["usage"]["total_tokens"]}（输入 {live["usage"]["prompt_tokens"]} / '
          f'输出 {live["usage"]["completion_tokens"]}）。R1 复用既有运行，其余五轮为本轮真实调用；'
          f'全部尝试（含被校验拒绝者）都在账本与事件链中，未删除任何一次。</p>')


def _live_rounds_table(live):
    """view-06：逐轮 I4/I5 来源与字符对照。"""
    if not live.get("available"):
        return ('<p class="note warn">I4 仍为编辑设计稿：六轮真实组织侧未完成，'
                '不得把编辑稿当作真实模型输出。</p>')
    rows = [[_esc(row["case_id"]), "逐字真实模型输出" if not row["reused"] else "逐字真实模型输出（复用）",
             row["organized_chars"], row["plain_chars"],
             f'{row["visible_chars"]["I4"]} / {row["visible_chars"]["I5"]}',
             _esc(row["prompt_variant"]),
             "是" if all(row["checks"].values()) else "否"] for row in live["cases"]]
    return _table(["轮次", "I4 内容来源", "I4 附加字符", "I5 附加字符", "可见总长 I4/I5",
                   "提示条件", "七项检查"], rows,
                  caption="I4 真实组织侧 / I5 确定性原句摘录（同证据块、10% 长度匹配）")


def _live_governance_table(live, collisions=None):
    """view-11：真实交付的治理与诚实门禁。"""
    if not live.get("available"):
        rows = [["六轮真实组织侧", "未完成或未通过核验", _esc(live.get("reason", "")), "—"]]
    else:
        gates = live["honest_gates"]
        rows = [
            ["六轮真实组织侧", "已完成 6/6", f'{live["case_count"]} 轮', live["manifest_sha256"][:16]],
            ["新增真实 HTTP 调用", "是", str(live["new_live_calls"]), "全部计入共享账本"],
            ["送受试送达", "否", str(gates["subject_delivered"]), "仅生成与配对，未送达"],
            ["真人语义审核", "未完成", str(gates["human_reviews_completed"]), "仍为待审"],
            ["完整 I4 资格", "否", str(gates["full_I4_eligible"]), "缺自然 T1 与真实送达"],
            ["正式实验资格", "否", str(gates["formal_eligible"]), "formal_launch_enabled=False"],
        ]
    collisions = collisions or []
    rows.append(["I5 与中性臂文本重合",
                 "已登记" if collisions else "无",
                 f"{len(collisions)} 处",
                 "、".join(f'{row["topic"]}/R{row["round"]}↔{row["arm"]}' for row in collisions)
                 or "—"])
    return _table(["治理项", "状态", "值", "说明"], rows,
                  caption="步骤39 真实调用的治理与诚实门禁")



_MICRO_TITLE = "真实 mimo-v2.6-flash 微型实证对照（TOPIC_01 环境 D：I0 vs I5 vs I4）"


def _micro_unavailable(micro):
    return (f'<h3>{_esc(_MICRO_TITLE)}</h3><p class="note warn">真实微型实证尚未完成或未通过核验：'
            f'{_esc(micro.get("reason", "unknown"))}；此处不展示任何推测数值。</p>')


def _micro_overview(micro):
    """view-01：真实微型实证摘要（与 1248 样本离线预演严格分开）。"""
    if not micro.get("available"):
        return _micro_unavailable(micro)
    spend = micro.get("step_spend") or {}
    sup = micro.get("superseded_runs") or []
    return (
        f'<h3>{_esc(_MICRO_TITLE)}</h3>'
        + _kpi([("受试 × 臂", "2 × 3"), ("真实调用（最终运行）", micro["actual_model_calls"]),
                ("被拒并重试的尝试", micro["rejected_attempts"]),
                ("最终运行费用 (¥)", micro["charged_cny"]),
                ("本步合计费用 (¥)", spend.get("step_charged_cny", "—")),
                ("账本累计已结算 (¥)", micro["billing_after_total_cny"])])
        + _table(["运行", "状态", "已结算请求", "费用(¥)", "说明"],
                 [[_esc(micro["run_id"]), "已完成（纳入清单）", "—", _esc(micro["charged_cny"]),
                   "16 份测量 + 16 份盲评全部回填"]]
                 + [[_esc(r["run_id"]), _esc(r["status"]), r["settle_count"], _esc(r["charged_cny"]),
                     _esc(r["reason"][:90])] for r in sup],
                 caption="步骤40 真实运行留痕（失败/中断运行不删除、费用如实并入）")
        + f'<p class="note">这是语言模型扮演的 2 名受试 × 3 臂机制演示，n=2，不做统计推断；'
          f'与上方 1248 样本确定性合成预演完全分开。清单 SHA-256 <code>{_esc(micro["manifest_sha256"])}</code>。</p>')


def _micro_trajectory(micro):
    """view-05：真实四时点轨迹与 T1 发言 / mirror / 干预回应原文。"""
    if not micro.get("available"):
        return _micro_unavailable(micro)
    rows = [[_esc(r["subject"]), _esc(r["arm"]), _esc(r["timepoint"]) + ("（共享父级）" if r["shared_parent"] else ""),
             _n(r["BS"], 4), _n(r["Acc"], 4), _n(r["S"], 4), _n(r["O"], 2), _n(r["U"], 2),
             _esc(r["blind_sample_id"])] for r in micro["comparison"]]
    blocks = []
    for s in micro["subjects"]:
        mirror_rows = [[_esc(it["section"]), _esc(it["text"]), _esc(it.get("user_quote") or "—"),
                        _esc(", ".join(it["citations"]))] for it in s["mirror_items"]]
        resp_rows = [[_esc(arm), f'R{index}', _esc(text)]
                     for arm in ARMS_MICRO
                     for index, text in enumerate(s["intervention_responses"].get(arm) or [], 1)]
        blocks.append(
            f'<h3>{_esc(s["subject"])}（初始立场 {"+1" if s["initial_side"] == "positive" else "−1"}）</h3>'
            f'<p class="note"><strong>真实 T1 茧房常规发言</strong>（{s["speech_attempts"]} 次尝试）：'
            f'{_esc(s["speech"])}</p>'
            + _table(["mirror 分区", "诊断原文", "用户原句（逐字子串）", "证据"], mirror_rows,
                     caption="真实 Stage 5 mirror 诊断（只作诊断记录，不送入分支）")
            + _table(["臂", "轮次", "受试单次回应原文"], resp_rows, caption="干预期单次简短回应"))
    return (f'<h3>{_esc(_MICRO_TITLE)}</h3>'
            + _table(["受试", "臂", "时点", "BS", "Acc", "S", "O", "U", "盲评样本"], rows,
                     caption="真实四时点指标（T0/T1 为分叉前共享父级测量；BS 越低越好）")
            + "".join(blocks))


def _micro_trace(micro):
    """view-07：真实节点 → 事件 → 响应哈希追溯。"""
    if not micro.get("available"):
        return _micro_unavailable(micro)
    rows = [[_esc(t["node_id"]), _esc(t["kind"]), _esc(t.get("subject") or "—"), _esc(t.get("arm") or "—"),
             _esc(t.get("timepoint") or "—"), t["attempts"], t["rejected"], t["visible_cards"],
             f'<code>{_esc((t.get("event_id") or "")[:12])}</code>',
             f'<code>{_esc((t.get("response_sha256") or "")[:12])}</code>'] for t in micro["trace"]]
    return (f'<h3>{_esc(_MICRO_TITLE)}</h3>'
            + _table(["节点", "类型", "受试", "臂", "时点", "尝试", "被拒", "可见卡", "事件ID", "响应SHA-256"],
                     rows, caption="真实调用节点追溯（每个被接受响应可回到事件链与原始 blob）"))


def _micro_coverage(micro):
    """view-10：真实 T3 的 O/U 与异见理解原文。"""
    if not micro.get("available"):
        return _micro_unavailable(micro)
    t3 = [[_esc(r["subject"]), _esc(r["arm"]), _n(r["O"], 2), _n(r["U"], 2)]
          for r in micro["comparison"] if r["timepoint"] == "T3"]
    texts = []
    for s in micro["subjects"]:
        for arm in ARMS_MICRO:
            view = s["t3_opposing_view"].get(arm) or {}
            texts.append([_esc(s["subject"]), _esc(arm), _esc(view.get("summary", "—")),
                          _esc(view.get("main_evidence", "—")), _esc(view.get("boundary", "—"))])
    return (f'<h3>{_esc(_MICRO_TITLE)}</h3>'
            + _table(["受试", "臂", "遗漏率 O", "异见理解 U"], t3, caption="真实 T3：六维覆盖遗漏与异见理解")
            + _table(["受试", "臂", "复述", "论据", "边界"], texts, caption="T3 异见理解原文摘录（盲评对象）"))


ARMS_MICRO = ("I0", "I5", "I4")


_MATRIX_TITLE = "三议题真实端到端实证矩阵（TOPIC_01 + TOPIC_02 + TOPIC_03 · mimo-v2.6-flash）"


def _matrix_unavailable(matrix):
    return (f'<h3>{_esc(_MATRIX_TITLE)}</h3><p class="note warn">三议题矩阵尚未完成或未通过核验：'
            f'{_esc(matrix.get("reason", "unknown"))}；此处不展示任何推测数值。</p>')


def _matrix_overview(matrix):
    """view-01：三议题矩阵总 KPI 与运行清单。"""
    if not matrix.get("available"):
        return _matrix_unavailable(matrix)
    tot = matrix["totals"]
    snap = matrix["ledger_snapshot_at_build"]
    rows = [[_esc(t["topic_id"]), _esc(t["topic_label"]),
             _esc(" / ".join(f'{s}({"+1" if t["subject_stances"][s] > 0 else "−1"})' for s in t["subjects"])),
             _esc(t["run_id"]), t["final_run_model_calls"], t["rejected_attempts"], _esc(t["charged_cny"]),
             f'{t["superseded_runs"]} 次 / ¥{_esc(t["superseded_charged_cny"])}',
             f'<code>{_esc(t["manifest_sha256"][:16])}</code>'] for t in matrix["topics"]]
    return (
        f'<h3>{_esc(_MATRIX_TITLE)}</h3>'
        + _kpi([("议题 × 受试 × 臂", f'{tot["topics"]} × 2 × 3 = {tot["branches"]} 条反事实分支'),
                ("采纳调用 / 实际调用", f'{tot["accepted_calls"]} / {tot["final_run_model_calls"]}'),
                ("真实测量 / 盲评", f'{tot["subject_measurements"]} / {tot["blind_evaluations"]}'),
                ("mirror / 干预回应", f'{tot["mirrors"]} / {tot["intervention_responses"]}'),
                ("三议题实证费用 (¥)", tot["all_live_micro_pilot_charged_cny"]),
                ("账本上限 / 已结算 (¥)", f'{snap["limit_cny"]} / {snap["charged_cny"]}')])
        + _table(["议题", "主题", "受试（初始立场）", "最终 Run ID", "调用", "被拒", "费用(¥)",
                  "被取代运行", "清单 SHA-256"], rows, caption="三议题真实运行清单（每议题 1 次完成运行纳入矩阵）")
        + f'<p class="note">受试规则：{_esc(matrix["subject_rule"])}（B02 区组内 S01 与 S05 未必对立，'
          f'故取 S01 与从 S05 起首个对立者）。n=2/议题，仅描述性对比，不做统计推断；'
          f'矩阵 SHA-256 <code>{_esc(matrix["matrix_sha256"])}</code>。</p>')


def _bs_svg(topic_id, rows):
    """单议题 BS 四时点小图：6 条线（2 受试 × 3 臂），纵轴按本议题数据自适应。"""
    colors = {"I0": "#64748b", "I5": "#0f766e", "I4": "#b45309"}
    dash = {0: "", 1: "5 3"}
    values = [r["BS"] for r in rows if r["BS"] is not None]
    if not values:
        return ""
    low, high = min(values), max(values)
    pad = (high - low) * 0.15 or max(high * 0.15, 0.001)
    low, high = max(0.0, low - pad), high + pad
    width, height, left, right, top, bottom = 300, 170, 46, 10, 14, 26
    def x(i):
        return left + i * (width - left - right) / 3
    def y(v):
        return top + (high - v) * (height - top - bottom) / (high - low)
    subjects = []
    for r in rows:
        if r["subject"] not in subjects:
            subjects.append(r["subject"])
    parts = [f'<svg class="mini-traj" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="{_esc(topic_id)} BS 四时点轨迹">',
             f'<line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#cbd5e1"/>',
             f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#cbd5e1"/>']
    for i, tp in enumerate(("T0", "T1", "T2", "T3")):
        parts.append(f'<text x="{x(i):.1f}" y="{height - 8}" font-size="10" text-anchor="middle" '
                     f'fill="#475569">{tp}</text>')
    for v in (low, high):
        parts.append(f'<text x="{left - 4}" y="{y(v) + 3:.1f}" font-size="9" text-anchor="end" '
                     f'fill="#475569">{v:.4f}</text>')
    for si, subject in enumerate(subjects):
        for arm in ARMS_MICRO:
            pts = [r for r in rows if r["subject"] == subject and r["arm"] == arm]
            pts.sort(key=lambda r: r["timepoint"])
            coords = " ".join(f'{x(i):.1f},{y(r["BS"]):.1f}' for i, r in enumerate(pts) if r["BS"] is not None)
            parts.append(f'<polyline points="{coords}" fill="none" stroke="{colors[arm]}" stroke-width="1.6" '
                         f'stroke-dasharray="{dash[si % 2]}" stroke-opacity="0.85"/>')
    parts.append("</svg>")
    return "".join(parts)


def _matrix_trajectories(matrix):
    """view-05：跨议题描述性对比、三议题 SVG 轨迹与 TOPIC_02/03 原文。"""
    if not matrix.get("available"):
        return _matrix_unavailable(matrix)
    desc_rows = [[_esc(r["topic_id"]), _esc(r["arm"]), _n(r["BS_T0_mean"], 4), _n(r["BS_T1_mean"], 4),
                  _n(r["BS_T2_mean"], 4), _n(r["BS_T3_mean"], 4), _n(r["delta_BS_T1_T3_mean"], 4),
                  _n(r["Acc_T3_mean"], 1), _n(r["S_T3_mean"], 1), _n(r["O_T3_mean"], 1), _n(r["U_T3_mean"], 1)]
                 for r in matrix["descriptive_by_topic_arm"]]
    figures = []
    for summary in matrix["topic_summaries"]:
        figures.append(
            f'<figure class="traj-card"><figcaption>{_esc(summary["topic_id"])} · '
            f'{_esc(summary["topic_label"])}</figcaption>{_bs_svg(summary["topic_id"], summary["comparison"])}'
            f'<div class="net-meta">实线={_esc(summary["subject_ids"][0])} · 虚线={_esc(summary["subject_ids"][1])}；'
            f'灰=I0 · 青=I5 · 橙=I4</div></figure>')
    details = []
    for summary in matrix["topic_summaries"]:
        if summary["topic_id"] == "TOPIC_01":
            continue   # TOPIC_01 原文已在上方单议题区块展示
        rows = [[_esc(r["subject"]), _esc(r["arm"]), _esc(r["timepoint"]) + ("（共享父级）" if r["shared_parent"] else ""),
                 _n(r["BS"], 4), _n(r["Acc"], 4), _n(r["S"], 4), _n(r["O"], 2), _n(r["U"], 2)]
                for r in summary["comparison"]]
        block = [f'<h3>{_esc(summary["topic_id"])} · {_esc(summary["topic_label"])}：真实四时点指标与原文</h3>',
                 _table(["受试", "臂", "时点", "BS", "Acc", "S", "O", "U"], rows,
                        caption=f'{summary["topic_id"]} 真实四时点（T0/T1 为分叉前共享父级测量）')]
        for s in summary["subjects"]:
            mirror_rows = [[_esc(it["section"]), _esc(it["text"]), _esc(it.get("user_quote") or "—"),
                            _esc(", ".join(it["citations"]))] for it in s["mirror_items"]]
            resp_rows = [[_esc(arm), f'R{i}', _esc(text)] for arm in ARMS_MICRO
                         for i, text in enumerate(s["intervention_responses"].get(arm) or [], 1)]
            block.append(
                f'<p class="note"><strong>{_esc(s["subject"])}（初始'
                f'{"+1" if s["initial_side"] == "positive" else "−1"}）真实 T1 常规发言：</strong>{_esc(s["speech"])}</p>'
                + _table(["mirror 分区", "诊断原文", "用户原句（逐字子串）", "证据"], mirror_rows,
                         caption=f'{summary["topic_id"]}/{s["subject"]} 真实 mirror 诊断（不送入分支）')
                + _table(["臂", "轮次", "受试单次回应原文"], resp_rows,
                         caption=f'{summary["topic_id"]}/{s["subject"]} 干预期回应'))
        details.append("".join(block))
    return (f'<h3>{_esc(_MATRIX_TITLE)}</h3>'
            + _table(["议题", "臂", "BS T0", "BS T1", "BS T2", "BS T3", "ΔBS T1→T3", "Acc T3", "S T3", "O T3", "U T3"],
                     desc_rows, caption="跨议题描述性对比（每格为 2 名受试均值；n=2，不做推断）")
            + '<div class="traj-grid">' + "".join(figures) + "</div>"
            + "".join(details))


def _matrix_trace(matrix):
    """view-07：TOPIC_02/03 真实节点追溯（TOPIC_01 见上表），合计三议题。"""
    if not matrix.get("available"):
        return _matrix_unavailable(matrix)
    out = [f'<h3>{_esc(_MATRIX_TITLE)}</h3>']
    for summary in matrix["topic_summaries"]:
        if summary["topic_id"] == "TOPIC_01":
            continue
        rows = [[_esc(t["node_id"]), _esc(t["kind"]), _esc(t.get("subject") or "—"), _esc(t.get("arm") or "—"),
                 _esc(t.get("timepoint") or "—"), t["attempts"], t["rejected"], t["visible_cards"],
                 f'<code>{_esc((t.get("event_id") or "")[:12])}</code>',
                 f'<code>{_esc((t.get("response_sha256") or "")[:12])}</code>'] for t in summary["trace"]]
        out.append(_table(["节点", "类型", "受试", "臂", "时点", "尝试", "被拒", "可见卡", "事件ID", "响应SHA-256"],
                          rows, caption=f'{summary["topic_id"]} 真实调用节点追溯（{len(rows)} 个节点 + 4 次四阶段调用）'))
    tot = matrix["totals"]
    out.append(f'<p class="note">三议题采纳 {tot["accepted_calls"]} 次调用（每议题 48 个节点 + 4 次四阶段分析），'
               f'另有 {tot["extra_calls_rejected_or_redone"]} 次被拒重试或四阶段重做的真实调用，均已记账；'
               f'最终运行实际调用合计 {tot["final_run_model_calls"]} 次。</p>')
    return "".join(out)


def _matrix_coverage(matrix):
    """view-10：三议题 T3 的 O/U 与 TOPIC_02/03 异见复述原文。"""
    if not matrix.get("available"):
        return _matrix_unavailable(matrix)
    ou = [[_esc(s["topic_id"]), _esc(r["subject"]), _esc(r["arm"]), _n(r["O"], 2), _n(r["U"], 2)]
          for s in matrix["topic_summaries"] for r in s["comparison"] if r["timepoint"] == "T3"]
    texts = []
    for s in matrix["topic_summaries"]:
        if s["topic_id"] == "TOPIC_01":
            continue
        for sub in s["subjects"]:
            for arm in ARMS_MICRO:
                view = sub["t3_opposing_view"].get(arm) or {}
                texts.append([_esc(s["topic_id"]), _esc(sub["subject"]), _esc(arm), _esc(view.get("summary", "—")),
                              _esc(view.get("main_evidence", "—")), _esc(view.get("boundary", "—"))])
    return (f'<h3>{_esc(_MATRIX_TITLE)}</h3>'
            + _table(["议题", "受试", "臂", "遗漏率 O", "异见理解 U"], ou, caption="三议题真实 T3：O 与 U")
            + _table(["议题", "受试", "臂", "复述", "论据", "边界"], texts,
                     caption="TOPIC_02/03 T3 异见理解原文（TOPIC_01 见上表）"))

# ---------------------------------------------------------------- 步骤42：双轨对比、封闭情境与路演导览
_DUAL_TITLE = "双轨实证对比专区：三议题现实常识天花板 vs §5.4 封闭情境去先验破茧（TOPIC_SYNTH_01 · synthetic）"
_SYNTH_TITLE = "§5.4 封闭虚构情境 TOPIC_SYNTH_01 星澜市：真实 mimo 四时点轨迹、误导题逐题概率与原文（synthetic）"
_TOUR_TITLE = "答辩路演聚光灯导览（Presentation Spotlight Tour）"
_DUAL_METRICS = ("BS", "Acc", "S", "O", "U")
_DUAL_TPS = ("T0", "T1", "T2", "T3")
_ARM_COLORS = {"I0": "#64748b", "I5": "#0f766e", "I4": "#b45309"}


def _dual_track(matrix, closed):
    """双轨对比数据：现实三议题按臂合并均值（每议题 2 受试均值再取三议题均值）vs 合成情境 2 受试均值。"""
    if not (matrix.get("available") and closed.get("available")):
        return {"available": False,
                "reason": "real_matrix_unavailable" if not matrix.get("available") else "closed_world_unavailable"}
    real_rows = matrix["descriptive_by_topic_arm"]
    real = []
    for arm in ARMS_MICRO:
        row = {"arm": arm}
        for metric in _DUAL_METRICS:
            for tp in _DUAL_TPS:
                row[f"{metric}_{tp}_mean"] = lmp._mean([r[f"{metric}_{tp}_mean"] for r in real_rows if r["arm"] == arm])
        real.append(row)
    synth = deepcopy(closed["descriptive_by_arm"])
    by = {r["arm"]: r for r in synth}
    t3_bs = {arm: by[arm]["BS_T3_mean"] for arm in ARMS_MICRO}
    t2_bs = {arm: by[arm]["BS_T2_mean"] for arm in ARMS_MICRO}
    best_t3 = min(t3_bs, key=lambda a: (t3_bs[a] is None, t3_bs[a]))
    best_t2 = min(t2_bs, key=lambda a: (t2_bs[a] is None, t2_bs[a]))
    min_t3 = [a for a in ARMS_MICRO if t3_bs[a] is not None and abs(t3_bs[a] - t3_bs[best_t3]) <= 1e-12]
    headline = {
        "real_T0_Acc_mean": real[0]["Acc_T0_mean"], "real_T0_BS_mean": real[0]["BS_T0_mean"],
        "synth_T0_Acc_mean": by["I0"]["Acc_T0_mean"], "synth_T0_BS_mean": by["I0"]["BS_T0_mean"],
        "synth_T1_Acc_mean": by["I0"]["Acc_T1_mean"], "synth_T1_BS_mean": by["I0"]["BS_T1_mean"],
        "synth_T1_S_mean": by["I0"]["S_T1_mean"],
        "synth_T1_misled_correct_mean": by["I0"]["misled_correct_T1_mean"],
        "synth_T0_misled_correct_mean": by["I0"]["misled_correct_T0_mean"],
        "synth_T2_BS_by_arm": t2_bs, "synth_T3_BS_by_arm": t3_bs,
        "synth_T3_Acc_by_arm": {arm: by[arm]["Acc_T3_mean"] for arm in ARMS_MICRO},
        "synth_T3_misled_correct_by_arm": {arm: by[arm]["misled_correct_T3_mean"] for arm in ARMS_MICRO},
        "misled_item_count": len(closed["misled_anchor_items"]),
        "synth_T3_O_by_arm": {arm: by[arm]["O_T3_mean"] for arm in ARMS_MICRO},
        "synth_T3_U_by_arm": {arm: by[arm]["U_T3_mean"] for arm in ARMS_MICRO},
        "synth_T3_transfer_correct_by_arm": {
            arm: lmp._mean([r["transfer_stats"]["correct"] for r in closed["item_level"]
                            if r["arm"] == arm and r["timepoint"] == "T3"]) for arm in ARMS_MICRO},
        "real_prior_ceiling": real[0]["Acc_T0_mean"] is not None and real[0]["Acc_T0_mean"] >= 99.0,
        "synth_t1_blinded": (by["I0"]["BS_T1_mean"] is not None and by["I0"]["BS_T0_mean"] is not None
                             and by["I0"]["BS_T1_mean"] > by["I0"]["BS_T0_mean"]),
        "synth_best_arm_T2": best_t2, "synth_best_arm_T3": best_t3, "synth_min_arms_T3": min_t3,
        "synth_I4_best_T3": min_t3 == ["I4"],
        "synth_I4_tied_best_T3": "I4" in min_t3 and len(min_t3) > 1,
        "synth_evidence_arms_beat_I0_T3": all(t3_bs[a] is not None and t3_bs["I0"] is not None
                                              and t3_bs[a] < t3_bs["I0"] for a in ("I5", "I4")),
    }
    return {"available": True, "reason": "verified", "metrics": list(_DUAL_METRICS), "timepoints": list(_DUAL_TPS),
            "real": {"label": "现实议题（TOPIC_01/02/03 合并）", "topics": [t["topic_id"] for t in matrix["topics"]],
                     "by_arm": real, "matrix_sha256": matrix["matrix_sha256"]},
            "synthetic": {"label": "§5.4 封闭情境 TOPIC_SYNTH_01（synthetic）", "by_arm": synth,
                          "manifest_sha256": closed["manifest_sha256"], "is_synthetic_closed_world": True},
            "headline": headline,
            "note": ("现实轨每格为三议题各 2 名受试均值的再平均；合成轨为 2 名受试均值。n 极小，"
                     "只作描述性机制演示，不做统计推断；合成情境结果不作为现实世界效果证据。")}


def _fmt(value, digits=3):
    return "—" if value is None else (f"{value:.{digits}f}" if isinstance(value, float) else str(value))


def _spotlight_tour(overview, trace_count, matrix, closed, dual, billing):
    """7 步答辩叙事（对齐 §12.3 “共同起点—不同曝光—干预分支—即时反应—延迟结果—总体估计—失败与限制”）。"""
    tot = matrix.get("totals") or {}
    h = dual.get("headline") or {}
    if dual.get("available"):
        t3 = h["synth_T3_BS_by_arm"]
        if h["synth_I4_best_T3"]:
            verdict = "I4 在 T3 的 BS 为三臂唯一最低（描述性领先，n=2 不作推断）"
        elif h["synth_I4_tied_best_T3"]:
            verdict = ("I4 与 " + "、".join(a for a in h["synth_min_arms_T3"] if a != "I4")
                       + " 并列最低：纠偏来自共同核查证据，未观察到组织反思段的额外客观增益")
        else:
            verdict = f"T3 的 BS 最低者为 {'、'.join(h['synth_min_arms_T3'])}，I4 未取得描述性领先"
        blinded = ("T1 出现茧房致盲（BS 高于 T0）" if h["synth_t1_blinded"]
                   else "T1 未出现预期的茧房致盲：测量层未采信网传误导数字，BS 与 T0 持平（发言层是否复述网传数字见 view-05 原文）")
        synth_line = (f"封闭情境 T0 的 BS={_fmt(h['synth_T0_BS_mean'], 4)}、Acc={_fmt(h['synth_T0_Acc_mean'], 1)}；"
                      f"经 8 轮同质+误导曝光后 T1 的 BS={_fmt(h['synth_T1_BS_mean'], 4)}、Acc={_fmt(h['synth_T1_Acc_mean'], 1)}、"
                      f"S={_fmt(h['synth_T1_S_mean'], 1)}，{blinded}。T3 三臂 BS："
                      f"I0={_fmt(t3['I0'], 4)}、I5={_fmt(t3['I5'], 4)}、I4={_fmt(t3['I4'], 4)}；{verdict}。"
                      f"探索性迁移题 T3 平均答对（共4题）：I0={_fmt(h['synth_T3_transfer_correct_by_arm']['I0'], 1)}、"
                      f"I5={_fmt(h['synth_T3_transfer_correct_by_arm']['I5'], 1)}、I4={_fmt(h['synth_T3_transfer_correct_by_arm']['I4'], 1)}；"
                      f"T3 遗漏率 O（越低越好）I5={_fmt(h['synth_T3_O_by_arm']['I5'], 1)}、I4={_fmt(h['synth_T3_O_by_arm']['I4'], 1)}，"
                      f"异见理解 U I5={_fmt(h['synth_T3_U_by_arm']['I5'], 1)}、I4={_fmt(h['synth_T3_U_by_arm']['I4'], 1)}。")
    else:
        synth_line = "封闭情境实证尚未完成或未通过核验，本步只展示设计，不展示任何推测数值。"
    real_acc = lmp._mean([r["Acc_T0_mean"] for r in matrix.get("descriptive_by_topic_arm") or [] if r["arm"] == "I0"])
    real_line = (f"三议题真实矩阵共 {tot.get('final_run_model_calls', '—')} 次调用、"
                 f"{tot.get('subject_measurements', '—')} 份测量与 {tot.get('blind_evaluations', '—')} 份盲评；"
                 f"三议题 T0 平均 Acc={_fmt(real_acc, 1)}，即预训练常识天花板。"
                 if matrix.get("available") else "三议题矩阵尚未完成或未通过核验。")
    steps = [
        {"key": "architecture", "title": "① 研究问题与 1248 样本总体架构", "view": "view-01", "target": "view-01",
         "speaker": "A 实验控制",
         "notes": [f"研究问题：在同质与误导信息流形成的茧房中，破茧助手（I4）能否比等证据原句摘录（I5）和保持原环境（I0）更好地修正判断。",
                   f"离线预演把 3 议题 × 区组 × 4 环境展开为 {overview['worlds']} 个曝光世界、{overview['branches']} 条六臂分支，"
                   f"回收 {overview['panel_samples']} 份面板样本，全部为确定性合成响应（is_demo=True）。",
                   "点击动作：在 view-01 指向 KPI 与“议题×区组×环境矩阵”表，强调这是工程预演而非真实结果。"]},
        {"key": "exposure", "title": "② 四环境曝光操纵与茧房形成", "view": "view-04", "target": "view-04",
         "speaker": "B 数据与真值",
         "notes": ["A/B 为平衡 2/2/2 配额，C/D 为同质 4/1/1；B/D 每轮 2 张错误卡，按冻结模式分散在三类立场。",
                   "茧房 = 同质配额 + 错误卡；环境 D 同时具备两者，是全部干预分支的起点。",
                   "点击动作：在 view-04 指向 X（异见曝光%）与错误卡比例两组柱。"]},
        {"key": "pairing", "title": "③ 六臂反事实分支与 I4/I5 等证据配对（步骤39）", "view": "view-06", "target": "view-06",
         "speaker": "C 助手适配",
         "notes": ["每个分支从同一 T1 父快照克隆，分叉前核心状态哈希一致，杜绝跨臂共享可变状态。",
                   "I4 与 I5 共用同一证据块、同一来源、同一回应提示，可见总长 600–800 字且长度差 ≤10%；差别只在组织与反思段。",
                   "点击动作：在 view-06 指向“I4/I5 等证据”列与步骤39 六轮真实配对表。"]},
        {"key": "real_matrix", "title": "④ 三议题真实端到端实证与先验天花板发现（步骤40-41）", "view": "view-01",
         "target": "spot-matrix", "speaker": "E 评分与统计",
         "notes": [real_line,
                   "关键发现：现实公共议题的锚题被 mimo-v2.6-flash 的预训练常识直接答对，T0 就已满分，三臂没有可改善空间——这是测量天花板，不是助手无效。",
                   "点击动作：在 view-01 指向三议题矩阵表，再切到 view-05 的三张 BS 小图。"]},
        {"key": "closed_world", "title": "⑤ 方案 §5.4 封闭情境去先验破茧逆转实证（步骤42）", "view": "view-01",
         "target": "spot-dualtrack", "secondary_target": "spot-synth", "speaker": "B 数据与真值 → C 助手适配",
         "notes": ["按 §5.4 封闭情境示例构建虚构“星澜市”两种交通方案，数字与核查证据固定，全部条目标 synthetic；受试被告知未读到数据时 p 取 0.45–0.55。",
                   synth_line,
                   "点击动作：先看双轨对比大表与折线，再点“跳到原文”查看 T1 被误导发言、mirror 诊断与 I4 回应原文。"]},
        {"key": "trace", "title": f"⑥ {trace_count} 条全链路哈希追溯与双盲评估", "view": "view-07", "target": "view-07",
         "speaker": "D 记录与可视化",
         "notes": [f"view-07 的 {trace_count} 条路径从来源 → 证据 → 卡片 → 受试引用 → 回答 → 盲评 → 网络逐节点可回放；真实运行另列节点 → 事件 → 响应 SHA-256。",
                   "盲评员只看到匿名 evidence_notes 与异见复述，不知道受试、臂与时点；注册表与面板可离线重建并逐哈希比对。",
                   "点击动作：在 view-07 展开任一路径的 details，展示节点值与哈希。"]},
        {"key": "governance", "title": "⑦ 预算账本、诚实门禁与研究边界", "view": "view-11", "target": "view-11",
         "speaker": "A 实验控制（收尾）",
         "notes": [f"账本累计已结算 ¥{billing['total_charged_cny']}，授权上限 ¥{billing['authorized_limit_cny']}；"
                   "每次真实请求先预留后结算，中断请求按最坏情况持有，不伪造结算。",
                   "边界：受试是语言模型、n 极小、方案A为 AI 代理审阅而非真人；formal_eligible 恒为 False，合成情境不作现实世界证据。",
                   "点击动作：在 view-11 指向 §15.1 门禁表与“真人CA 未验证”KPI，收束到下一步真人复核。"]},
    ]
    for n, step in enumerate(steps, 1):
        step["step"] = n
    return {"title": _TOUR_TITLE, "steps": steps,
            "narrative_basis": "§12.3 共同起点—不同曝光—干预分支—即时反应—延迟结果—总体估计—失败与限制",
            "keyboard": "← / → 切换步骤；Esc 收起提词卡"}


def _tour_nav(tour):
    buttons = "".join(
        f'<button type="button" class="tour-step" data-step="{s["step"] - 1}" data-target="{_esc(s["target"])}" '
        f'title="{_esc(s["title"])}">{_esc(s["title"][:1])}<b>{_esc(s["title"][2:12])}</b></button>'
        for s in tour["steps"])
    return (f'<nav class="tour" id="spotlight-tour" aria-label="{_esc(tour["title"])}">'
            f'<strong class="tour-label">路演导览</strong>'
            f'<button type="button" id="tour-prev" class="tour-nav">上一步</button>{buttons}'
            f'<button type="button" id="tour-next" class="tour-nav">下一步</button>'
            f'<button type="button" id="tour-toggle" class="tour-nav">提词卡</button></nav>')


def _tour_notes_html(step):
    lines = "".join(f"<li>{_esc(line)}</li>" for line in step["notes"])
    extra = (f'<button type="button" class="tour-jump" data-target="{_esc(step["secondary_target"])}">跳到原文</button>'
             if step.get("secondary_target") else "")
    return (f'<div class="tour-notes-head"><strong>{_esc(step["title"])}</strong>'
            f'<em>主讲：{_esc(step["speaker"])}</em></div><ul>{lines}</ul>{extra}')


def _tour_panel(tour):
    return (f'<aside class="tour-notes" id="tour-notes" aria-live="polite" aria-label="现场答辩解说提词卡">'
            f'<div class="tour-notes-kicker">现场答辩解说提词卡（Speaker Notes）</div>'
            f'<div id="tour-notes-body">{_tour_notes_html(tour["steps"][0])}</div></aside>')


def _tour_script(tour):
    body = "".join(f'<div class="tour-script-step" id="tour-script-{s["step"]}">{_tour_notes_html(s)}</div>'
                   for s in tour["steps"])
    return (f'<details class="tour-script" id="tour-script"><summary>{_esc(tour["title"])}：7 步提词全文'
            f'（打印/无脚本备用；叙事依据 {_esc(tour["narrative_basis"])}）</summary>{body}</details>')


def _dual_svg(dual, metric):
    """单指标双轨小图：左=现实三议题合并、右=合成情境；两轨共用同一纵轴范围（§12.4 轴范围一致）。"""
    tracks = (("现实", dual["real"]["by_arm"]), ("合成", dual["synthetic"]["by_arm"]))
    values = [r[f"{metric}_{tp}_mean"] for _l, rows in tracks for r in rows for tp in _DUAL_TPS
              if r[f"{metric}_{tp}_mean"] is not None]
    if metric == "BS":
        low, high = 0.0, max([0.3] + [v * 1.1 for v in values])
    else:
        low, high = 0.0, 100.0
    width, height, top, bottom, left, gap = 340, 150, 16, 24, 40, 18
    panel = (width - left - gap - 8) / 2
    parts = [f'<svg class="dual-traj" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="{_esc(metric)} 双轨四时点对比">']
    for t, (label, rows) in enumerate(tracks):
        x0 = left + t * (panel + gap)
        def x(i, x0=x0):
            return x0 + i * panel / 3
        def y(v):
            return top + (high - v) * (height - top - bottom) / ((high - low) or 1)
        parts.append(f'<rect x="{x0 - 4:.1f}" y="{top - 4}" width="{panel + 8:.1f}" height="{height - top - bottom + 8}" '
                     f'fill="{"#f8fafc" if t == 0 else "#fff7ed"}" stroke="#e2e8f0"/>')
        parts.append(f'<text x="{x0:.1f}" y="11" font-size="10" fill="#334155">{_esc(label)}</text>')
        for i, tp in enumerate(_DUAL_TPS):
            parts.append(f'<text x="{x(i):.1f}" y="{height - 8}" font-size="9" text-anchor="middle" fill="#475569">{tp}</text>')
        for row in rows:
            pts = [(i, row[f"{metric}_{tp}_mean"]) for i, tp in enumerate(_DUAL_TPS) if row[f"{metric}_{tp}_mean"] is not None]
            coords = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in pts)
            width_px = {"I0": 3.2, "I5": 2.4, "I4": 1.6}[row["arm"]]
            parts.append(f'<polyline class="draw" points="{coords}" fill="none" stroke="{_ARM_COLORS[row["arm"]]}" '
                         f'stroke-width="{width_px}" stroke-opacity="0.9" stroke-linejoin="round"/>')
            for i, v in pts:
                parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.4" fill="{_ARM_COLORS[row["arm"]]}">'
                             f'<title>{_esc(label)} {_esc(row["arm"])} {_DUAL_TPS[i]} {_esc(metric)}={v:.4f}</title></circle>')
    for v in (low, high):
        yy = top + (high - v) * (height - top - bottom) / ((high - low) or 1)
        parts.append(f'<text x="{left - 4}" y="{yy + 3:.1f}" font-size="9" text-anchor="end" fill="#475569">'
                     f'{v:.2f}</text>' if metric == "BS" else
                     f'<text x="{left - 4}" y="{yy + 3:.1f}" font-size="9" text-anchor="end" fill="#475569">{v:.0f}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _dual_section(dual):
    """view-01：双轨对比大表 + 五指标双轨折线（聚光灯目标 spot-dualtrack）。"""
    head = f'<h3>{_esc(_DUAL_TITLE)}</h3>'
    if not dual.get("available"):
        return (f'<div class="spot-target dualtrack" id="spot-dualtrack">{head}<p class="note warn">双轨对比尚不可用：'
                f'{_esc(dual.get("reason", "unknown"))}；此处不展示任何推测数值。</p></div>')
    h = dual["headline"]
    rows = []
    for label, track in (("现实三议题", dual["real"]["by_arm"]), ("合成 synthetic", dual["synthetic"]["by_arm"])):
        for r in track:
            rows.append([_esc(label), _esc(r["arm"])]
                        + [_n(r[f"BS_{tp}_mean"], 4) for tp in _DUAL_TPS]
                        + [_n(r[f"Acc_{tp}_mean"], 1) for tp in _DUAL_TPS]
                        + [_n(r[f"S_{tp}_mean"], 1) for tp in ("T1", "T3")]
                        + [_n(r["O_T3_mean"], 1), _n(r["U_T3_mean"], 1)])
    figs = "".join(f'<figure class="dual-card"><figcaption>{_esc(m)}（{"越低越好" if m in ("BS", "S", "O") else "越高越好"}）'
                   f'</figcaption>{_dual_svg(dual, m)}</figure>' for m in _DUAL_METRICS)
    t3 = h["synth_T3_BS_by_arm"]
    return (
        f'<div class="spot-target dualtrack" id="spot-dualtrack">{head}'
        + _kpi([("现实轨 T0 Acc", _fmt(h["real_T0_Acc_mean"], 1)),
                ("合成轨 T0 BS / Acc", f'{_fmt(h["synth_T0_BS_mean"], 4)} / {_fmt(h["synth_T0_Acc_mean"], 1)}'),
                ("合成轨 T1 BS / Acc", f'{_fmt(h["synth_T1_BS_mean"], 4)} / {_fmt(h["synth_T1_Acc_mean"], 1)}'),
                ("合成轨 T3 BS（I0/I5/I4）", f'{_fmt(t3["I0"], 4)} / {_fmt(t3["I5"], 4)} / {_fmt(t3["I4"], 4)}'),
                ("T3 BS 最低臂", " = ".join(h["synth_min_arms_T3"])),
                ("被误导锚题答对（T1→T3·I4）",
                 f'{_fmt(h["synth_T1_misled_correct_mean"], 1)} → {_fmt(h["synth_T3_misled_correct_by_arm"]["I4"], 1)}'
                 f' / {h["misled_item_count"]}'),
                ("迁移题 T3 答对（I0/I5/I4，共4）",
                 " / ".join(_fmt(h["synth_T3_transfer_correct_by_arm"][a], 1) for a in ARMS_MICRO))])
        + _table(["轨道", "臂", "BS T0", "BS T1", "BS T2", "BS T3", "Acc T0", "Acc T1", "Acc T2", "Acc T3",
                  "S T1", "S T3", "O T3", "U T3"], rows,
                 caption="双轨并排大表（T0/T1 为分叉前共享父级；BS/S/O 越低越好，Acc/U 越高越好）")
        + '<div class="dual-grid">' + figs + '</div>'
        + '<p class="note">图例：灰（最粗）=I0 保持原环境 · 青（中）=I5 等证据原句摘录 · 橙（最细）=I4 破茧助手，线宽不同以便重合时仍可辨认；'
          '左格=现实三议题合并，右格（暖底）=合成情境；同一指标两轨纵轴一致。</p>'
        + f'<p class="note warn">{_esc(dual["note"])} 合成清单 SHA-256 <code>{_esc(dual["synthetic"]["manifest_sha256"])}</code>。</p>'
        + '</div>')


def _synth_unavailable(closed):
    return (f'<h3>{_esc(_SYNTH_TITLE)}</h3><p class="note warn">封闭情境实证尚未完成或未通过核验：'
            f'{_esc(closed.get("reason", "unknown"))}；此处不展示任何推测数值。</p>')


def _p(value):
    return "—" if value is None else f"{value:.2f}"


def _synth_section(closed):
    """view-05：封闭情境四时点、逐题概率、T1 发言 / mirror / I4 组织段 / 三臂回应原文（聚光灯 spot-synth）。"""
    if not closed.get("available"):
        return f'<div class="spot-target" id="spot-synth">{_synth_unavailable(closed)}</div>'
    rows = [[_esc(r["subject"]), _esc(r["arm"]), _esc(r["timepoint"]) + ("（共享父级）" if r["shared_parent"] else ""),
             _n(r["BS"], 4), _n(r["Acc"], 1), _n(r["S"], 1), _n(r["O"], 2), _n(r["U"], 2), _esc(r["blind_sample_id"])]
            for r in closed["comparison"]]
    level = {(r["subject"], r["arm"], r["timepoint"]): r["p_true"] for r in closed["item_level"]}
    item_rows = []
    for item in closed["item_design"]:
        cells = [_esc(item["item_id"]) + ("" if item["partition"] == "anchor" else "（迁移）"),
                 _esc(item["statement"]), "真" if item["truth"] else "假",
                 _esc({"pro_A_rumor": "亲甲网传", "pro_B_rumor": "亲乙网传", None: "—"}[item["misled_by"]])]
        for subject in ("S01", "S02"):
            seq = [level.get((subject, None, "T0"), {}).get(item["item_id"]),
                   level.get((subject, None, "T1"), {}).get(item["item_id"])]
            seq += [level.get((subject, arm, "T3"), {}).get(item["item_id"]) for arm in ARMS_MICRO]
            cells.append(" → ".join(_p(v) for v in seq[:2]) + "｜" + " / ".join(_p(v) for v in seq[2:]))
        item_rows.append(cells)
    blocks = []
    for s in closed["subjects"]:
        mirror_rows = [[_esc(it["section"]), _esc(it["text"]), _esc(it.get("user_quote") or "—"),
                        _esc(", ".join(it["citations"]))] for it in s["mirror_items"]]
        add_rows = [[f"R{r}", _esc(s["i4_additions"][r]), _esc(s["i5_additions"][r]),
                     "通过" if all(s["pair_checks"][r][k] for k in ("evidence_equal", "source_equal", "dose_within_range",
                                                                    "length_matched", "additional_length_matched",
                                                                    "response_equal", "assembly_valid")) else "未通过"]
                    for r in sorted(s["i4_additions"])]
        resp_rows = [[_esc(arm), f"R{i}", _esc(text)] for arm in ARMS_MICRO
                     for i, text in enumerate(s["intervention_responses"].get(arm) or [], 1)]
        misled = closed["misled_anchor_items"]
        reason_rows = [[_esc(q), _esc(s["t1_brief_reasons"].get(q, "—"))]
                       + [_esc(s["t3_brief_reasons"][arm].get(q, "—")) for arm in ARMS_MICRO] for q in misled]
        blocks.append(
            f'<h3>{_esc(s["subject"])}（初始立场 {"+1 倾向甲方案快速公交" if s["initial_side"] == "positive" else "−1 倾向乙方案氢能智轨"}）'
            f'<span class="pill synth">synthetic</span></h3>'
            f'<p class="note"><strong>T1 茧房常规发言（真实 mimo）：</strong>{_esc(s["speech"])}</p>'
            + _table(["mirror 分区", "认知偏差诊断原文", "用户原句（逐字子串）", "证据"], mirror_rows,
                     caption=f'{s["subject"]} 真实 mirror 诊断（交给 I4 组织段，不直接送入分支）')
            + _table(["轮次", "I4 破茧助手组织与反思段（真实 mimo）", "I5 等证据原句摘录（确定性）", "七项配对"], add_rows,
                     caption=f'{s["subject"]} I4/I5 阅读说明并排（共同证据块相同，未重复展示）')
            + _table(["臂", "轮次", "受试回应原文（真实 mimo）"], resp_rows,
                     caption=f'{s["subject"]} 干预期回应：I4 臂即“顿悟回应”候选，需人工语义复核')
            + _table(["被误导锚题", "T1 理由", "T3·I0 理由", "T3·I5 理由", "T3·I4 理由"], reason_rows,
                     caption=f'{s["subject"]} 被误导锚题的自报理由（模型自报，不等于认知过程）'))
    figure = (f'<div class="traj-grid"><figure class="traj-card"><figcaption>TOPIC_SYNTH_01 · BS 四时点</figcaption>'
              f'{_bs_svg("TOPIC_SYNTH_01", closed["comparison"])}<div class="net-meta">实线=S01 · 虚线=S02；'
              f'灰=I0 · 青=I5 · 橙=I4</div></figure></div>')
    spend = closed.get("step_spend") or {}
    return (
        f'<div class="spot-target" id="spot-synth"><h3>{_esc(_SYNTH_TITLE)}</h3>'
        + _kpi([("真实调用", closed["actual_model_calls"]), ("被拒重试", closed["rejected_attempts"]),
                ("本步费用 (¥)", spend.get("step_charged_cny", closed["charged_cny"])),
                ("协议依据", closed["protocol_basis"]), ("合成标记", "is_synthetic_closed_world=True")])
        + f'<p class="note warn">{_esc(closed["protocol_note"])}</p>'
        + figure
        + _table(["受试", "臂", "时点", "BS", "Acc", "S", "O", "U", "盲评样本"], rows,
                 caption="封闭情境真实四时点指标（16 份测量 + 16 份独立盲评回填；T0/T1 为共享父级）")
        + _table(["题", "陈述（synthetic）", "真值", "误导口径", "S01：T0→T1｜T3 I0/I5/I4", "S02：T0→T1｜T3 I0/I5/I4"],
                 item_rows, caption="逐题 p_true 轨迹（8 锚题主终点 + 4 迁移题探索；≥0.5 判为“真”；被误导锚题的 T1 概率反映是否被网传数字带偏）")
        + "".join(blocks) + "</div>")


def _synth_trace(closed):
    if not closed.get("available"):
        return _synth_unavailable(closed)
    rows = [[_esc(t["node_id"]), _esc(t["kind"]), _esc(t.get("subject") or "—"), _esc(t.get("arm") or "—"),
             _esc(t.get("timepoint") or "—"), t["attempts"], t["rejected"],
             f'<code>{_esc((t.get("event_id") or "")[:12])}</code>',
             f'<code>{_esc((t.get("response_sha256") or "")[:12])}</code>'] for t in closed["trace"]]
    return (f'<h3>{_esc(_SYNTH_TITLE)}</h3>'
            + _table(["节点", "类型", "受试", "臂", "时点", "尝试", "被拒", "事件ID", "响应SHA-256"], rows,
                     caption=f'封闭情境真实调用节点追溯（{len(rows)} 个节点 + 四阶段调用；synthetic）'))


def _sections(dataset):
    overview = dataset["overview"]
    checklist = overview["checklist"]
    billing = overview["billing"]
    out = {}
    out["view-01"] = (
        _kpi([("曝光世界", overview["worlds"]), ("六臂分支", overview["branches"]),
              ("面板样本", overview["panel_samples"]), ("网络记录", overview["network_records"]),
              ("缺失个体", overview["missing_agents_total"]),
              ("审核槽位", f'{checklist["human_slots_completed"]}/110')])
        + _table(["议题", "区组", "曝光世界", "六臂分支", "面板样本", "网络记录", "环境"],
                 [[_esc(row["topic_id"]), _esc(row["block_id"]), row["worlds"], row["branches"],
                   row["panel_samples"], row["network_records"], _esc("".join(row["environments"]))]
                  for row in overview["matrix"]], caption="议题×区组×环境矩阵")
        + _table(["方案A项", "值"],
                 [["签发行来源", _esc(checklist["review_origin"])],
                  ["完成槽位 / 项目", f'{checklist["human_slots_completed"]} / {checklist["items_total"]}'],
                  ["通过项目", checklist["items_passed"]],
                  ["初审分歧 / 未决", f'{checklist["disagreement_count"]} / {checklist["unresolved_disagreements"]}'],
                  ["角色列完成数", checklist["human_reviews_completed"]],
                  ["放行状态", "已解锁（方案A代理）" if checklist["pilot_release_unlocked"] else "锁定"],
                  ["真人CA签名", "未验证（恒为 False）"]], caption="方案A 55项/110槽签发状态")
        + _table(["账本", "值"],
                 [["已结算调用", billing["settled_calls"]],
                  ["请求组", billing["request_groups"]],
                  ["累计费用 (¥)", billing["total_charged_cny"]],
                  ["授权上限 (¥)", billing["authorized_limit_cny"]],
                  ["剩余 (¥)", billing["remaining_cny"]]], caption="调用与成本账本")
        + _table(["失败运行", "状态", "错误码", "明细", "保留"],
                 [[_esc(row["run_id"]), _esc(row["status"]), _esc(row["error_code"]),
                   _esc(json.dumps(row["detail"], ensure_ascii=False)), "是"] for row in overview["failures"]],
                 caption="历史失败留痕（不隐藏 v1/v2/v3 短投递诊断）")
        + _table(["验证报告", "测试数", "失败", "错误", "成功"],
                 [[_esc(row["file"]), row["tests_run"], row["failures"], row["errors"],
                   "是" if row["success"] else "否"] for row in overview["milestones"]],
                 caption="全项目测试里程碑（最近 8 次）")
        + _live_delivery_summary(dataset["live_delivery"])
        + _micro_overview(dataset["live_micro_pilot"])
        + '<div class="spot-target" id="spot-matrix">' + _matrix_overview(dataset["live_micro_matrix"]) + "</div>"
        + _dual_section(dataset["dual_track"])
    )
    timeline_cards = []
    for block in dataset["timeline"]:
        cells = []
        for step in block["steps"]:
            cells.append(f'<span class="chip {step["phase"]}">{_esc(step["label"])}'
                         f'<small>{step["logical_seq"]}{"·" + step["sha256"][:8] if step["sha256"] else ""}</small></span>')
        timeline_cards.append(
            '<div class="timeline-row">'
            f'<div class="timeline-head"><strong>{_esc(block["topic"])} / {_esc(block["block"])}</strong>'
            f'<small>{len(block["steps"])} 个阶段节点</small></div>'
            '<div class="chips">' + "".join(cells) + '</div></div>')
    out["view-02"] = (
        '<div class="timeline">' + "".join(timeline_cards) + "</div>"
        + '<p class="note">胶囊首行为阶段名，次行为逻辑序号与快照哈希前 8 位；'
          '同一区组的阶段节点横向流式排列，窄屏自动折行，胶囊自身不断裂。</p>')
    net_cards = []
    for node in dataset["network"]:
        net_cards.append(
            f'<figure class="net-card"><figcaption>{_esc(node["topic"])} / {_esc(node["block"])}</figcaption>'
            f'{_svg_network(node)}'
            f'<div class="net-meta">同立场边 {node["same_stance_edges"]} · '
            f'异立场边 {node["opposite_stance_edges"]}</div>'
            f'<div class="net-meta">{_esc(node["note"])}</div></figure>')
    out["view-03"] = (
        '<div class="net-grid">' + "".join(net_cards) + "</div>"
        + '<p class="note">蓝=初始立场 +1，橙=−1；实线=同立场关注，虚线=异立场关注；'
          '严格受控层回复不跨人实时传播。</p>')
    out["view-04"] = (
        _table(["环境", "X（异见曝光%）", "Hn（熵）", "错误卡比例", "最大重复", "配额", "每轮卡数"],
               [[_esc(row["environment"]), _n(row["X"], 3), _n(row["Hn"], 4),
                 _n(row["error_ratio"], 3), row["max_repeat"],
                 _esc(json.dumps(row["quota"], ensure_ascii=False)), row["cards_per_round"]]
                for row in dataset["info_flow"]], caption="四环境信息流操纵检查")
        + _svg_bars([(f'{row["environment"]} · X', row["X"], "#2563eb") for row in dataset["info_flow"]]
                    + [(f'{row["environment"]} · 错误%', 100 * row["error_ratio"], "#d97706")
                       for row in dataset["info_flow"]], max_value=100)
        + f'<p class="note">{_esc(dataset["info_flow"][0]["note"])}</p>')
    case_rows = []
    for name, label in (("improved", "改善"), ("unchanged", "无变化"), ("reversed", "反向")):
        case = dataset["cases"]["cases"].get(name)
        if case is None:
            case_rows.append([label, "不存在该类案例（按固定 seed 如实标注）", "—", "—", "—", "—"])
            continue
        key = case["trace_key"]
        case_rows.append([
            label,
            f'T1 BS {_n(case["T1"]["BS"])} → T3 BS {_n(case["T3"]["BS"])}（Δ {_n(case["delta_BS"])}）',
            f'{_esc(key["topic_id"])} / {_esc(key["block_id"])} / {_esc(key["arm"])} / {_esc(key["agent_id"])}',
            _esc(key["item_id"]), _esc(key["card_id"]),
            f'{_esc(case["T1"]["sid"])} → {_esc(case["T3"]["sid"])}'])
    out["view-05"] = (
        _table(["案例", "轨迹", "追溯键（议题/区组/臂/个体）", "题目", "证据卡", "盲评样本ID"], case_rows,
               caption="§12.3 三类典型案例（只作解释，总体效应包含全部运行）")
        + f'<p class="note">{_esc(dataset["cases"]["seed_rule"])}</p>'
        + ("".join(f'<p class="note warn">{_esc(note)}</p>' for note in dataset["cases"]["notes"]))
        + f'<p class="note">个体轨迹共 {len(dataset["series"])} 条；下方交互区可按筛选器查看。</p>'
        + _micro_trajectory(dataset["live_micro_pilot"])
        + _matrix_trajectories(dataset["live_micro_matrix"])
        + _synth_section(dataset["closed_world_pilot"])
    )
    packet_rows = []
    groups = {}
    for item in dataset["interventions"]:
        groups.setdefault((item["topic"], item["round"]), {})[item["arm"]] = item
    for (topic, round_no), arms in sorted(groups.items()):
        i4, i5 = arms.get("I4"), arms.get("I5")
        matched_pair = (i4 and i5 and i4["evidence_block_sha256"] == i5["evidence_block_sha256"]
                        and abs(i4["additional_chars"] - i5["additional_chars"])
                        <= 0.1 * min(i4["additional_chars"], i5["additional_chars"]))
        match = "是" if matched_pair else "否"
        for arm in sr.ARMS:
            item = arms[arm]
            packet_rows.append([f'{_esc(topic)} / R{round_no}', _esc(arm), item["char_count"],
                                _esc(", ".join(item["evidence_ids"])), match,
                                "是" if item["extractive_constraint_satisfied"] else "否",
                                "是" if not item["contains_treatment_label"] else "否"])
    out["view-06"] = (
        _table(["议题 / 轮次", "臂", "字符数", "证据ID", "I4/I5 等证据", "摘录约束", "无臂标签"],
               packet_rows, caption="六臂干预包并排对照（600–800 字符，单次回应机会）")
        + f'<p class="note">I4/I5 共同证据块与附加段长度逐轮匹配；可见文本全文见数据包 interventions 字段。</p>'
        + _live_rounds_table(dataset["live_delivery"]))
    trace_rows = []
    for path in dataset["trace_paths"]:
        key = path["key"]
        chain = " → ".join(node["type"] for node in path["nodes"])
        trace_rows.append([path["path_id"], _esc(path["kind"]),
                           f'{_esc(key.get("topic_id", ""))} / {_esc(key.get("block_id", ""))}',
                           _esc(key.get("arm") or "—"), _esc(key.get("agent_id", "")),
                           _esc(key.get("timepoint", "")), _esc(key.get("item_id") or "—"),
                           _esc(key.get("evidence_id") or "—"), _esc(chain)])
    out["view-07"] = (
        _table(["路径", "类型", "议题/区组", "臂", "个体", "时点", "题目", "证据", "链路"], trace_rows,
               caption=f'全链路追溯表（{len(dataset["trace_paths"])} 条；边表示可观察引用/依赖，不声称认知因果）')
        + _table(["层", "数量"],
                 [["真实取证来源", len(dataset["evidence"]["sources"])],
                  ["摘编证据", len(dataset["evidence"]["items"])],
                  ["受试可见卡（曝光+保持）", len(dataset["evidence"]["cards"])],
                  ["干预包（六臂×两轮×三议题）", len(dataset["interventions"])],
                  ["追溯路径", len(dataset["trace_paths"])]], caption="证据层规模")
        + '<p class="note">来源 URL 以纯文本呈现，不做外链加载；点击链路需在数据包中按 path_id 定位。</p>'
        + _micro_trace(dataset["live_micro_pilot"])
        + _matrix_trace(dataset["live_micro_matrix"])
        + _synth_trace(dataset["closed_world_pilot"]))
    exposure = dataset["effects"]["exposure"]
    out["view-08"] = (
        _table(["议题", "同质效应", "错误信息效应", "交互效应", "可估", "完成区组"],
               [[_esc(tid), _n(row["contrasts"]["homogeneous"] if row["contrasts"] else None),
                 _n(row["contrasts"]["misinformation"] if row["contrasts"] else None),
                 _n(row["contrasts"]["interaction"] if row["contrasts"] else None),
                 "是" if row["estimable"] else "否", row["complete_blocks"]]
                for tid, row in exposure["per_topic"].items()], caption="2×2 曝光效应（网络级，T1−T0）")
        + _table(["终点", "比较", "效应", "95% CI", "p_raw", "Holm p_adjusted", "n/期望"],
                 [[_esc(tp), _esc(name), _n(item["effect"]), _esc(f'[{_n(item["ci_95"][0])}, {_n(item["ci_95"][1])}]'
                                                                  if item["ci_95"] else "—"),
                   _n(item["p_raw"], 4), _n(item["p_adjusted"], 4), f'{item["n"]}/{item["expected_n"]}']
                  for tp in ("T2", "T3")
                  for name, item in dataset["effects"]["intervention"][tp]["comparisons"].items()],
                 caption="六臂配对效应（主确认性比较）")
        + _svg_forest(exposure, dataset["effects"]["intervention"])
        + f'<p class="note">{_esc(dataset["effects"]["note"])}</p>')
    calib = dataset["calibration"]
    def calib_rows(node):
        rows = []
        for index, label in enumerate(node["bin_labels"]):
            rows.append([label,
                         f'{node["anchor"]["correct"][index]} / {node["anchor"]["incorrect"][index]}',
                         f'{node["transfer"]["correct"][index]} / {node["transfer"]["incorrect"][index]}'])
        rows.append(["合计", f'{node["anchor_n"]}（期望 {node["anchor_expected_n"]}）',
                     f'{node["transfer_n"]}（期望 {node["transfer_expected_n"]}）'])
        return rows
    out["view-09"] = (
        _table(["信心区间", "锚题 正确/错误", "迁移题 正确/错误"], calib_rows(calib["overall"]),
               caption="总体：正误×信心分布（锚题8题主终点；迁移题4题探索性，分母不合并）")
        + "".join(_table(["信心区间", "锚题 正确/错误", "迁移题 正确/错误"], calib_rows(node),
                         caption=f'{tid}：正误×信心分布')
                  for tid, node in calib["per_topic"].items())
        + f'<p class="note">{_esc(calib["overall"]["denominator_note"])}</p>')
    coverage_rows = []
    for tid, node in dataset["coverage"].items():
        entry = node.get("T3")
        if not entry:
            continue
        coverage_rows.append([_esc(tid), entry["n"], _n(entry["mastered_rate"], 2),
                              _n(entry["gap_rate"], 2), _n(entry["missed_rate"], 2),
                              _n(entry["U_mean"], 2), _n(entry["u_parts_mean"][0], 2),
                              _n(entry["u_parts_mean"][1], 2), _n(entry["u_parts_mean"][2], 2)])
    out["view-10"] = (
        _table(["议题", "n", "掌握率%", "识别缺口%", "遗漏率 O", "U 均值",
                "复述", "论据", "边界"], coverage_rows, caption="六维覆盖与异见理解 U（T3 主终点）")
        + _svg_bars([(f'{tid} · 掌握', node["T3"]["mastered_rate"], "#2563eb")
                     for tid, node in dataset["coverage"].items() if node.get("T3")]
                    + [(f'{tid} · 识别缺口', node["T3"]["gap_rate"], "#0f766e")
                       for tid, node in dataset["coverage"].items() if node.get("T3")]
                    + [(f'{tid} · 遗漏 O', node["T3"]["missed_rate"], "#b91c1c")
                       for tid, node in dataset["coverage"].items() if node.get("T3")], max_value=100)
        + '<p class="note">O 拆分：掌握 + 识别缺口 + 遗漏 = 100；U 三分项为复述/论据/边界各 0–2 分。</p>'
        + _micro_coverage(dataset["live_micro_pilot"])
        + _matrix_coverage(dataset["live_micro_matrix"]))
    gov = dataset["governance"]
    gate_rows = [[_esc(name), "通过" if value is True else ("未通过" if value is False else _esc(value))]
                 for name, value in gov["acceptance_gates"].items()]
    out["view-11"] = (
        _kpi([("引用契约通过", f'{gov["citation"]["schema_valid"]}/{gov["citation"]["mirror_checks"]}'),
              ("初审分歧", len(gov["disputes"])), ("方案A来源", gov["review_origin"]),
              ("真人CA", "未验证")])
        + _table(["争议条目", "检查项", "初审A", "初审B", "最终", "裁决依据"],
                 [[_esc(row["item_id"]), _esc(row["check"]), _esc(row["reviewer_1_decision"]),
                   _esc(row["reviewer_2_decision"]), _esc(row["final_decision"]),
                   _esc(row["resolution_basis"])] for row in gov["disputes"]],
                 caption="方案A双视角 4 项争议与第三人裁决")
        + _table(["§15.1 门禁", "结果"], gate_rows, caption="运行前验收门禁")
        + _table(["议题", "来源", "层级", "可再分发", "许可说明"],
                 [[_esc(row["topic"]), _esc(row["source_id"]), _esc(row["source_level"]),
                   "否" if row["redistribution_cleared"] is False else _esc(row["redistribution_cleared"]),
                   _esc(str(row["license_note"])[:70])]
                  for row in dataset["evidence"]["rights_warnings"]],
                 caption="来源权利预警（逐源未核验，不作法律结论）")
        + _live_governance_table(dataset["live_delivery"], dataset["dose_collisions"])
        + f'<p class="note">语义蕴含：{_esc(gov["citation"]["semantic_entailment"])}；'
          f'方案A不是真人审核，formal_eligible 恒为 False。</p>')
    return out


_CSS = """
:root{--bg:#f6f8fb;--panel:#ffffff;--ink:#0f172a;--muted:#475569;--line:#e2e8f0;
--blue:#2563eb;--orange:#d97706;--teal:#0f766e;--red:#b91c1c;--violet:#7c3aed;
--radius:10px}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC","Microsoft YaHei",sans-serif;
font-size:14px;line-height:1.55}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
font-size:11px;overflow-wrap:anywhere}
/* 顶栏：banner 与 filters 同处一个 sticky 容器，banner 折行变高时不再遮挡下方筛选器 */
.topbar{position:sticky;top:0;z-index:50;background:var(--bg);
box-shadow:0 8px 16px -14px rgba(15,23,42,.55)}
.banner{background:#7f1d1d;color:#fff;padding:10px 16px;
font-weight:700;letter-spacing:.2px;border-bottom:3px solid #fca5a5}
.banner small{display:block;font-weight:500;opacity:.92;margin-top:3px;font-size:11px;line-height:1.45}
.filters{background:rgba(248,250,252,.98);border-bottom:1px solid var(--line);padding:8px 16px;
display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.filters label{font-size:12px;color:var(--muted);display:inline-flex;align-items:center;gap:5px}
.filters .meta{flex:1 1 100%;font-size:11px;line-height:1.5}
header{padding:20px 16px 8px}
h1{font-size:22px;margin:0 0 6px;line-height:1.3}
h2{font-size:17px;margin:0 0 4px;padding-bottom:6px;border-bottom:2px solid var(--line)}
h3{font-size:14px;margin:14px 0 6px;color:var(--muted)}
.meta{color:var(--muted);font-size:12px;overflow-wrap:anywhere}
select{padding:5px 8px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:var(--ink);font-size:13px}
button{padding:5px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;cursor:pointer}
button:hover{border-color:#94a3b8}
main{padding:10px 16px 40px;max-width:1400px;margin:0 auto}
section.view{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
padding:14px 16px;margin:16px 0;box-shadow:0 1px 2px rgba(15,23,42,.04);min-width:0}
.desc{color:var(--muted);font-size:12px;margin:4px 0 10px}
.static{min-width:0}
/* 宽表格横向滚动保护：长哈希/长文本不再撑破卡片 */
.table-wrap{width:100%;max-width:100%;overflow-x:auto;overflow-y:hidden;
margin:8px 0 12px;border-radius:8px;border:1px solid var(--line)}
.table-wrap>table.tbl{border:0;margin:0;min-width:520px}
table.tbl{width:100%;border-collapse:collapse;font-size:12px}
table.tbl caption{text-align:left;font-weight:700;color:var(--muted);padding:6px 8px}
table.tbl th,table.tbl td{border:1px solid var(--line);padding:5px 7px;vertical-align:top;text-align:left;
overflow-wrap:anywhere;word-break:break-word}
table.tbl th{background:#f1f5f9;font-weight:600;white-space:nowrap}
table.tbl tr:nth-child(even) td{background:#fcfdff}
.kpi{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 12px}
.kpi-item{flex:1 1 140px;min-width:0;border:1px solid var(--line);border-radius:8px;
padding:8px 10px;background:#f8fafc}
.kpi-label{font-size:11px;color:var(--muted)}
.kpi-value{font-size:19px;font-weight:700;overflow-wrap:anywhere}
.note{color:var(--muted);font-size:12px;margin:6px 0}
.note.warn{color:#92400e;background:#fffbeb;border-left:3px solid #f59e0b;padding:6px 8px;border-radius:4px}
/* 时间轴：每个区组一张卡片，阶段胶囊在同一行内横向流式排列，自动折行 */
.timeline{display:flex;flex-direction:column;gap:10px;margin:8px 0 6px}
.timeline-row{display:grid;grid-template-columns:158px minmax(0,1fr);gap:8px 14px;align-items:start;
border:1px solid var(--line);border-radius:var(--radius);background:#fff;padding:10px 12px}
.timeline-head{display:flex;flex-direction:column;gap:2px;min-width:0}
.timeline-head strong{font-size:12px;letter-spacing:.2px}
.timeline-head small{display:block;color:var(--muted);font-size:10px;line-height:1.4}
.chips{display:flex;flex-wrap:wrap;align-items:flex-start;gap:8px;min-width:0}
/* .chip 必须是块级/弹性盒：避免行内 span 内嵌 display:block 的 small 触发 inline-box 拆裂 */
.chip{display:inline-flex;flex-direction:column;align-items:flex-start;justify-content:center;
vertical-align:middle;gap:1px;border:1px solid var(--line);border-left:3px solid var(--accent,#cbd5e1);
border-radius:10px;padding:5px 10px;font-size:11px;line-height:1.3;background:#f8fafc;
white-space:nowrap;max-width:100%}
.chip small{display:block;color:var(--muted);font-size:9px;line-height:1.25;
font-variant-numeric:tabular-nums}
.chip.exposure{--accent:#3b82f6;border-color:#bfdbfe;background:#eff6ff}
.chip.snapshot{--accent:#6366f1;border-color:#c7d2fe;background:#eef2ff}
.chip.branch{--accent:#8b5cf6;border-color:#ddd6fe;background:#f5f3ff}
.chip.intervention{--accent:#f59e0b;border-color:#fde68a;background:#fffbeb}
.chip.measurement{--accent:#10b981;border-color:#a7f3d0;background:#ecfdf5}
.chip.retention{--accent:#ef4444;border-color:#fecaca;background:#fef2f2}
/* 网络回放：6 个拓扑图并排网格，窄屏自动降列 */
.net-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin:10px 0 6px}
.net-card{margin:0;display:flex;flex-direction:column;align-items:center;gap:6px;
border:1px solid var(--line);border-radius:var(--radius);background:#fff;padding:10px}
.net-card figcaption{font-weight:700;font-size:12px;align-self:flex-start}
.net-card svg.net{width:100%;max-width:240px;height:auto;display:block}
.net-meta{align-self:flex-start;font-size:10px;color:var(--muted);line-height:1.45}
/* 步骤41：三议题真实 BS 四时点小图 */
.traj-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:10px 0 6px}
.traj-card{margin:0;display:flex;flex-direction:column;align-items:center;gap:6px;
border:1px solid var(--line);border-radius:var(--radius);background:#fff;padding:10px}
.traj-card figcaption{font-weight:700;font-size:12px;align-self:flex-start}
.traj-card svg.mini-traj{width:100%;max-width:320px;height:auto;display:block}
svg.forest,svg.bars{width:100%;max-width:640px;height:auto}
.live{margin-top:10px;border-top:1px dashed var(--line);padding-top:8px;
overflow-x:auto;overflow-y:hidden;min-width:0}
.live table.tbl{margin:6px 0}
.pill{display:inline-block;border-radius:999px;padding:1px 8px;font-size:11px;
border:1px solid var(--line);background:#fff;white-space:nowrap}
footer{color:var(--muted);font-size:11px;padding:0 16px 24px}
/* 步骤42：答辩路演聚光灯导览（顶栏内单行、窄屏折行）+ 固定提词卡 + 双轨对比 */
.tour{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:6px 16px;
background:linear-gradient(90deg,#1e293b,#334155);border-bottom:2px solid #f59e0b}
.tour-label{color:#fde68a;font-size:12px;letter-spacing:.5px;margin-right:4px}
.tour button{border:1px solid #64748b;background:#0f172a;color:#e2e8f0;font-size:12px;
padding:4px 9px;border-radius:999px;display:inline-flex;align-items:center;gap:4px;max-width:100%}
.tour button b{font-weight:500}
.tour button:hover{border-color:#fbbf24;color:#fff}
.tour button.active{background:#f59e0b;border-color:#f59e0b;color:#111827;font-weight:700}
.tour .tour-nav{background:#334155}
section.view,.spot-target{scroll-margin-top:170px}
.spotlight{outline:3px solid #f59e0b;outline-offset:4px;border-radius:10px;
animation:spot-pulse 1.6s ease-in-out 2}
@keyframes spot-pulse{0%{box-shadow:0 0 0 0 rgba(245,158,11,.55)}70%{box-shadow:0 0 0 14px rgba(245,158,11,0)}
100%{box-shadow:0 0 0 0 rgba(245,158,11,0)}}
.tour-notes{position:fixed;right:14px;bottom:14px;z-index:60;width:min(440px,calc(100vw - 28px));
max-height:46vh;overflow:auto;background:#fffbeb;border:1px solid #fcd34d;border-left:5px solid #f59e0b;
border-radius:12px;padding:10px 12px;box-shadow:0 12px 30px -12px rgba(15,23,42,.45);font-size:13px}
.tour-notes.hidden{display:none}
.tour-notes-kicker{font-size:11px;color:#92400e;font-weight:700;letter-spacing:.3px;margin-bottom:4px}
.tour-notes-head{display:flex;flex-direction:column;gap:2px;margin-bottom:4px}
.tour-notes-head em{font-style:normal;color:#b45309;font-size:12px}
.tour-notes ul{margin:4px 0 6px;padding-left:18px}
.tour-notes li{margin:3px 0;line-height:1.5}
.tour-jump{background:#f59e0b;border-color:#f59e0b;color:#111827;font-weight:700}
.tour-script{margin-top:8px;border:1px dashed #fcd34d;border-radius:8px;padding:6px 10px;background:#fffdf5}
.tour-script-step{border-top:1px dashed #fde68a;padding:6px 0}
.dualtrack{border:2px solid #fdba74;border-radius:12px;padding:8px 12px;margin-top:14px;
background:linear-gradient(180deg,#fff7ed,#ffffff 120px)}
.dual-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin:10px 0 6px}
.dual-card{margin:0;border:1px solid var(--line);border-radius:var(--radius);background:#fff;padding:8px;
display:flex;flex-direction:column;gap:4px}
.dual-card figcaption{font-weight:700;font-size:12px}
.dual-card svg.dual-traj{width:100%;height:auto;display:block}
svg.dual-traj polyline.draw{stroke-dasharray:600;stroke-dashoffset:600;animation:draw-line 1.8s ease-out forwards}
@keyframes draw-line{to{stroke-dashoffset:0}}
.pill.synth{margin-left:6px;background:#fff7ed;border-color:#fdba74;color:#9a3412}
@media (prefers-reduced-motion:reduce){svg.dual-traj polyline.draw{animation:none;stroke-dashoffset:0}
.spotlight{animation:none}}
details{margin:4px 0}
summary{cursor:pointer;color:var(--blue)}
@media (min-width:1100px){.net-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:900px){.net-grid{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}}
@media (max-width:760px){.topbar{position:static}.kpi-item{flex:1 1 45%}
.timeline-row{grid-template-columns:1fr}
main{padding:8px 10px 32px}section.view{padding:12px 12px}}
@media print{.filters,.tour,.tour-notes{display:none}.tour-script{display:block}
svg.dual-traj polyline.draw{animation:none;stroke-dashoffset:0}body{background:#fff}section.view{break-inside:avoid;box-shadow:none}
.topbar{position:static}.net-grid{grid-template-columns:repeat(3,1fr)}}
"""

_JS = r"""
(function(){
  const D=JSON.parse(document.getElementById('dashboard-data').textContent);
  const S={topic:'all',block:'all',env:'all',arm:'all',tp:'all',agent:'all'};
  const $=function(id){return document.getElementById(id);};
  const esc=function(v){return String(v===null||v===undefined?'':v).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});};
  const num=function(v,d){ if(v===null||v===undefined)return '—';
    if(typeof v==='boolean')return v?'是':'否';
    if(typeof v==='number'){ if(!isFinite(v))return '—';
      return Math.abs(v)>=1000? v.toFixed(1): (Math.round(v*Math.pow(10,d||3))/Math.pow(10,d||3)).toString(); }
    return esc(v);};
  function match(r){
    return (S.topic==='all'||r.topic===S.topic)&&(S.block==='all'||r.block===S.block)&&
      (S.env==='all'||r.env===S.env)&&(S.arm==='all'||String(r.arm)===S.arm)&&
      (S.tp==='all'||r.tp===S.tp)&&(S.agent==='all'||r.agent===S.agent);
  }
  function filtered(){return D.panel.filter(match);}
  function seriesFiltered(){
    return D.series.filter(function(s){
      return (S.topic==='all'||s.topic===S.topic)&&(S.block==='all'||s.block===S.block)&&
        (S.env==='all'||s.env===S.env)&&(S.arm==='all'||String(s.arm)===S.arm)&&
        (S.agent==='all'||s.agent===S.agent);
    });
  }
  function status(){
    const rows=filtered();
    $('live-status').textContent='当前筛选：议题 '+S.topic+' · 区组 '+S.block+' · 环境 '+S.env+
      ' · 臂 '+S.arm+' · 时点 '+S.tp+' · 个体 '+S.agent+' → 样本 '+rows.length+
      '，世界 '+new Set(rows.map(function(r){return r.world;})).size+' 个';
  }
  function spark(points){
    const order={T0:0,T1:1,T2:2,T3:3};
    const pts=points.slice().sort(function(a,b){return order[a.tp]-order[b.tp];});
    if(!pts.length)return '';
    const w=150,h=42,pad=6;
    const xs=function(i){return pad+i*(w-2*pad)/Math.max(1,pts.length-1);};
    const ys=function(v){return h-pad-(Math.max(0,Math.min(1,v)))*(h-2*pad);};
    let path='',dots='';
    pts.forEach(function(p,i){
      path+=(i?' L':'M')+xs(i).toFixed(1)+' '+ys(p.BS).toFixed(1);
      const color=p.S>0?'#b91c1c':'#2563eb';
      dots+='<circle cx="'+xs(i).toFixed(1)+'" cy="'+ys(p.BS).toFixed(1)+'" r="3" fill="'+color+'"><title>'+
        esc(p.tp+' BS '+num(p.BS,3)+' Acc '+num(p.Acc,1)+' S '+num(p.S,1)+' sid '+p.sid)+'</title></circle>';
    });
    return '<svg viewBox="0 0 150 42" width="150" height="42" role="img"><path d="'+path+
      '" fill="none" stroke="#94a3b8" stroke-width="1.2"/>'+dots+'</svg>';
  }
  function tblWrap(inner){return '<div class="table-wrap">'+inner+'</div>';}
  function renderSeries(){
    const list=seriesFiltered().slice(0,80);
    $('live-05').innerHTML='<p class="note">共 '+seriesFiltered().length+
      ' 条轨迹，最多显示 80 条；红点表示该时点 S&gt;0（答错且高信心）。</p>'+
      tblWrap('<table class="tbl"><thead><tr><th>议题/区组</th><th>世界</th><th>臂</th><th>个体</th><th>轨迹</th><th>T1→T3 ΔBS</th></tr></thead><tbody>'+
      list.map(function(s){
        const t1=s.points.find(function(p){return p.tp==='T1';});
        const t3=s.points.find(function(p){return p.tp==='T3';});
        const d=(t1&&t3)?num(t3.BS-t1.BS,4):'—';
        return '<tr><td>'+esc(s.topic+' / '+s.block)+'</td><td>'+esc(s.world)+'</td><td>'+esc(s.arm||'—')+
          '</td><td>'+esc(s.agent)+'</td><td>'+spark(s.points)+'</td><td>'+d+'</td></tr>';
      }).join('')+'</tbody></table>');
  }
  function renderInterventions(){
    const rows=D.interventions.filter(function(r){
      return (S.topic==='all'||r.topic===S.topic)&&(S.arm==='all'||r.arm===S.arm);
    });
    $('live-06').innerHTML='<p class="note">筛选后 '+rows.length+
      ' 份干预包；字符数须在 600–800，且不含臂标签。</p>'+tblWrap('<table class="tbl"><tbody>'+
      rows.map(function(r){
        return '<tr><td>'+esc(r.topic+' R'+r.round)+'</td><td>'+esc(r.arm)+'</td><td>'+r.char_count+
          '</td><td>'+esc(r.evidence_ids.join(', '))+'</td><td>'+esc(r.visible_text_sha256.slice(0,12))+
          '</td></tr>';
      }).join('')+'</tbody></table>');
  }
  function renderTrace(){
    const rows=D.trace_paths.filter(function(p){
      const k=p.key;
      return (S.topic==='all'||k.topic_id===S.topic)&&(S.block==='all'||k.block_id===S.block)&&
        (S.arm==='all'||String(k.arm)===S.arm)&&(S.agent==='all'||k.agent_id===S.agent)&&
        (S.tp==='all'||k.timepoint===S.tp);
    });
    $('live-07').innerHTML='<p class="note">筛选后 '+rows.length+
      ' 条可追溯路径（共 '+D.trace_paths.length+' 条）。</p>'+
      rows.map(function(p){
        return '<details><summary>'+esc(p.path_id+' · '+p.kind+' · '+(p.key.topic_id||''))+
          '</summary>'+tblWrap('<table class="tbl"><tbody>'+p.nodes.map(function(n){
            return '<tr><td>'+esc(n.type)+'</td><td>'+esc(n.id)+'</td><td><code>'+
              esc(JSON.stringify(n.value).slice(0,240))+'</code></td></tr>';
          }).join('')+'</tbody></table>')+'</details>';
      }).join('');
  }
  function renderCoverage(){
    const tids=(S.topic==='all')?Object.keys(D.coverage):[S.topic];
    const rows=[];
    tids.forEach(function(tid){
      const node=D.coverage[tid]||{};const e=node.T3;const e1=node.T1;
      if(!e)return;
      rows.push('<tr><td>'+esc(tid)+'</td><td>'+num(e1?e1.BS||e1.O_mean:null,2)+'</td><td>'+num(e.O_mean,2)+
        '</td><td>'+num(e.mastered_rate,2)+'</td><td>'+num(e.gap_rate,2)+'</td><td>'+num(e.U_mean,2)+
        '</td><td>'+num(e.u_parts_mean[0],2)+' / '+num(e.u_parts_mean[1],2)+' / '+num(e.u_parts_mean[2],2)+'</td></tr>');
    });
    $('live-10').innerHTML=tblWrap('<table class="tbl"><thead><tr><th>议题</th><th>T1 O</th><th>T3 O</th><th>掌握%</th>'+
      '<th>识别缺口%</th><th>U</th><th>复述/论据/边界</th></tr></thead><tbody>'+rows.join('')+'</tbody></table>');
  }
  function renderCalibration(){
    const tid=(S.topic==='all')?'overall':S.topic;
    const node=(tid==='overall')?D.calibration.overall:(D.calibration.per_topic[tid]||D.calibration.overall);
    let html='<table class="tbl"><thead><tr><th>信心区间</th><th>锚题 正确/错误</th><th>迁移题 正确/错误</th></tr></thead><tbody>';
    node.bin_labels.forEach(function(label,i){
      html+='<tr><td>'+esc(label)+'</td><td>'+node.anchor.correct[i]+' / '+node.anchor.incorrect[i]+
        '</td><td>'+node.transfer.correct[i]+' / '+node.transfer.incorrect[i]+'</td></tr>';
    });
    html+='<tr><td>合计</td><td>'+node.anchor_n+' / 期望 '+node.anchor_expected_n+'</td><td>'+
      node.transfer_n+' / 期望 '+node.transfer_expected_n+'</td></tr></tbody></table>';
    $('live-09').innerHTML=tblWrap(html);
  }
  function renderTimeline(){
    const rows=D.timeline.filter(function(b){
      return (S.topic==='all'||b.topic===S.topic)&&(S.block==='all'||b.block===S.block);
    });
    $('live-02').innerHTML='<p class="note">筛选后 '+rows.length+
      ' 个区组时间轴；逻辑序号与快照哈希来自预演记录，墙钟时间只用于耗时。</p>'+
      '<div class="timeline">'+rows.map(function(b){
        return '<div class="timeline-row"><div class="timeline-head"><strong>'+
          esc(b.topic+' / '+b.block)+'</strong><small>'+b.steps.length+
          ' 个阶段节点</small></div><div class="chips">'+b.steps.map(function(s){
            return '<span class="chip '+s.phase+'">'+esc(s.label)+'<small>'+s.logical_seq+
              (s.sha256?' · '+esc(s.sha256.slice(0,8)):'')+'</small></span>';
          }).join('')+'</div></div>';
      }).join('')+'</div>';
  }
  function renderNetwork(){
    const rows=D.network.filter(function(n){
      return (S.topic==='all'||n.topic===S.topic)&&(S.block==='all'||n.block===S.block);
    });
    $('live-03').innerHTML='<p class="note">筛选后 '+rows.length+
      ' 个基础网络；拓扑图见上方静态网格，六臂共享同一坐标与初始立场。</p>'+
      tblWrap('<table class="tbl"><thead><tr><th>议题 / 区组</th><th>同立场边</th><th>异立场边</th>'+
      '<th>节点（立场）</th></tr></thead><tbody>'+rows.map(function(n){
        return '<tr><td>'+esc(n.topic+' / '+n.block)+'</td><td>'+n.same_stance_edges+
          '</td><td>'+n.opposite_stance_edges+'</td><td>'+n.nodes.map(function(x){
            return esc(x.agent_id+'('+(x.stance>0?'+1':'-1')+')');}).join(' ')+'</td></tr>';
      }).join('')+'</tbody></table>');
  }
  function renderOverviewLive(){
    const rows=filtered();
    $('live-01').innerHTML='<p class="note">筛选样本 '+rows.length+
      ' 条；其中完整行 '+rows.filter(function(r){return r.ok;}).length+' 条。</p>';
  }
  function renderFlow(){
    const rows=D.info_flow.map(function(r){
      const dim=(S.env==='all'||S.env===r.environment)?'':' style="opacity:.35"';
      return '<tr'+dim+'><td>'+esc(r.environment)+'</td><td>'+num(r.X,3)+'</td><td>'+num(r.Hn,4)+
        '</td><td>'+num(100*r.error_ratio,2)+'%</td><td>'+esc(JSON.stringify(r.quota))+'</td></tr>';
    });
    $('live-04').innerHTML=tblWrap('<table class="tbl"><thead><tr><th>环境</th><th>X</th><th>Hn</th>'+
      '<th>错误卡</th><th>配额</th></tr></thead><tbody>'+rows.join('')+'</tbody></table>');
  }
  function renderEffects(){
    const rows=[];
    ['T2','T3'].forEach(function(tp){
      Object.keys(D.effects.intervention[tp].comparisons).forEach(function(name){
        const c=D.effects.intervention[tp].comparisons[name];
        rows.push('<tr><td>'+tp+'</td><td>'+esc(name)+'</td><td>'+num(c.effect,4)+
          '</td><td>'+num(c.p_raw,4)+'</td><td>'+num(c.p_adjusted,4)+'</td></tr>');
      });
    });
    $('live-08').innerHTML='<p class="note">固定议题集合的确认性比较；筛选只影响下钻，不改变总体估计。</p>'+
      tblWrap('<table class="tbl"><thead><tr><th>终点</th><th>比较</th><th>效应</th><th>p_raw</th>'+
      '<th>Holm p_adj</th></tr></thead><tbody>'+rows.join('')+'</tbody></table>');
  }
  function renderGovernance(){
    const map={TOPIC_01:'CL',TOPIC_02:'EN',TOPIC_03:'WE'};
    const rows=D.governance.disputes.filter(function(d){
      return S.topic==='all'||d.item_id.indexOf(map[S.topic])>=0;
    });
    $('live-11').innerHTML='<p class="note">筛选后 '+rows.length+' 条争议（共 '+
      D.governance.disputes.length+' 条）；全部由第三人裁决闭环。</p><ul>'+
      rows.map(function(d){return '<li>'+esc(d.item_id+' · '+d.check+' · 初审B '+d.reviewer_2_decision+
        ' → 最终 '+d.final_decision)+'</li>';}).join('')+'</ul>';
  }
  function renderAll(){
    status();
    try{renderOverviewLive();}catch(e){}
    try{renderTimeline();}catch(e){}
    try{renderNetwork();}catch(e){}
    try{renderFlow();}catch(e){}
    try{renderSeries();}catch(e){}
    try{renderInterventions();}catch(e){}
    try{renderTrace();}catch(e){}
    try{renderEffects();}catch(e){}
    try{renderCalibration();}catch(e){}
    try{renderCoverage();}catch(e){}
    try{renderGovernance();}catch(e){}
  }
  document.querySelectorAll('select[data-filter]').forEach(function(sel){
    sel.addEventListener('change',function(){S[sel.getAttribute('data-filter')]=sel.value;renderAll();});
  });
  var reset=document.getElementById('reset-filters');
  if(reset){reset.addEventListener('click',function(){
    Object.keys(S).forEach(function(k){S[k]='all';});
    document.querySelectorAll('select[data-filter]').forEach(function(sel){sel.value='all';});
    renderAll();});}
  renderAll();
  function initTour(){
    const T=D.spotlight_tour; const panel=$('tour-notes'); const body=$('tour-notes-body');
    if(!T||!T.steps||!panel||!body)return;
    let cur=0;
    function noteHtml(st){
      return '<div class="tour-notes-head"><strong>'+esc(st.title)+'</strong><em>主讲：'+esc(st.speaker)+
        '</em></div><ul>'+st.notes.map(function(n){return '<li>'+esc(n)+'</li>';}).join('')+'</ul>'+
        (st.secondary_target?'<button type="button" class="tour-jump" data-target="'+esc(st.secondary_target)+
        '">跳到原文</button>':'');
    }
    function focusTarget(id,scroll){
      document.querySelectorAll('.spotlight').forEach(function(el){el.classList.remove('spotlight');});
      const el=document.getElementById(id);
      if(!el)return;
      el.classList.add('spotlight');
      if(scroll&&el.scrollIntoView){el.scrollIntoView({behavior:'smooth',block:'start'});}
    }
    function go(i,scroll){
      cur=(i+T.steps.length)%T.steps.length;
      const st=T.steps[cur];
      document.querySelectorAll('.tour-step').forEach(function(b){
        b.classList.toggle('active',Number(b.getAttribute('data-step'))===cur);});
      body.innerHTML=noteHtml(st);
      panel.classList.remove('hidden');
      focusTarget(st.target,scroll);
    }
    document.querySelectorAll('.tour-step').forEach(function(b){
      b.addEventListener('click',function(){go(Number(b.getAttribute('data-step')),true);});});
    const prev=$('tour-prev'),next=$('tour-next'),toggle=$('tour-toggle');
    if(prev){prev.addEventListener('click',function(){go(cur-1,true);});}
    if(next){next.addEventListener('click',function(){go(cur+1,true);});}
    if(toggle){toggle.addEventListener('click',function(){panel.classList.toggle('hidden');});}
    panel.addEventListener('click',function(ev){
      const t=ev.target;
      if(t&&t.classList&&t.classList.contains('tour-jump')){focusTarget(t.getAttribute('data-target'),true);}
    });
    document.addEventListener('keydown',function(ev){
      const tag=(ev.target&&ev.target.tagName)||'';
      if(tag==='SELECT'||tag==='INPUT'||tag==='TEXTAREA')return;
      if(ev.key==='ArrowRight'){go(cur+1,true);}
      else if(ev.key==='ArrowLeft'){go(cur-1,true);}
      else if(ev.key==='Escape'){panel.classList.add('hidden');}
    });
    go(0,false);
  }
  try{initTour();}catch(e){}
})();
"""

_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title>
<style>@@CSS@@</style>
</head>
<body>
<div class="topbar" id="topbar">
<div class="banner" role="alert" id="top-banner">@@BANNER@@
<small>只读派生看板 · 不写入原始日志 · 不加载任何外部脚本或样式 · 案例只作解释，总体效应包含全部运行</small></div>
<nav class="filters" aria-label="全局筛选器">
@@FILTERS@@
<button id="reset-filters" type="button">重置</button>
<span class="meta" id="live-status"></span>
</nav>
@@TOUR@@
</div>
<header>
<h1>@@TITLE@@</h1>
<p class="meta">@@META@@</p>
@@TOURSCRIPT@@
</header>
<main>
@@SECTIONS@@
</main>
@@TOURNOTES@@
<footer>
<p>@@FOOTER@@</p>
<p>本页面为单文件自包含只读看板：无外部 CDN、无远程字体、无网络请求；数据来源于 dashboard_data.json。</p>
</footer>
<script id="dashboard-data" type="application/json">@@DATA@@</script>
<script>@@JS@@</script>
</body>
</html>
"""


def _filters(dataset):
    def options(values, label, key):
        items = [f'<option value="all">全部{_esc(label)}</option>']
        for value in values:
            items.append(f'<option value="{_esc(value)}">{_esc(value)}</option>')
        return (f'<label>{_esc(label)} <select data-filter="{key}">' + "".join(items) + "</select></label>")
    params = dataset["parameters"]
    return "".join((
        options(params["topics"], "议题", "topic"),
        options([f"B{index + 1:02d}" for index in range(params["blocks_per_topic"])], "区组", "block"),
        options(params["environments"], "环境", "env"),
        options(params["arms"], "干预臂", "arm"),
        options(params["timepoints"], "时点", "tp"),
        options([f"S{index:02d}" for index in range(1, params["agents_per_world"] + 1)], "个体", "agent"),
    ))


def render_experiment_dashboard(output_html_path, dataset) -> Path:
    """渲染单文件自包含、响应式并可打印的只读看板。"""
    _need(type(dataset) is dict and dataset.get("contract") == DATASET_VERSION, "dataset_invalid")
    body = {key: value for key, value in dataset.items() if key != "dataset_sha256"}
    _need(dataset.get("dataset_sha256") == _sha(body), "dataset_hash_mismatch")
    _need(dataset["banner"]["text"] == BANNER_TEXT, "banner_text_changed")
    sections = _sections(dataset)
    html_sections = []
    for view_id, title, description in VIEWS:
        html_sections.append(
            f'<section class="view" id="{view_id}" data-view="{view_id}">'
            f'<h2>{_esc(title)}</h2><p class="desc">{_esc(description)}</p>'
            f'<div class="static">{sections[view_id]}</div>'
            f'<div class="live" id="live-{view_id.split("-")[1]}"></div></section>')
    data_json = json.dumps(dataset, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    meta = (f'数据包 SHA-256 <code>{_esc(dataset["dataset_sha256"])}</code> · '
            f'生成时间 {_esc(dataset["generated_at_utc"])} · '
            f'seed {dataset["parameters"]["seed"]} · '
            f'{dataset["parameters"]["blocks_per_topic"]} 区组 × '
            f'{dataset["parameters"]["agents_per_world"]} 人 · '
            f'rehearsal_mode {_esc(dataset["honest_gates"]["rehearsal_mode"])}')
    footer = (f'方案A来源 {_esc(dataset["governance"]["review_origin"])}；'
              f'真人CA签名 {_esc(dataset["honest_gates"]["real_human_ca_signature_verified"])}；'
              f'formal_eligible {_esc(dataset["honest_gates"]["formal_eligible"])}。'
              f' 费用：累计 ¥{_esc(dataset["overview"]["billing"]["total_charged_cny"])} / '
              f'上限 ¥{_esc(dataset["overview"]["billing"]["authorized_limit_cny"])}。')
    html = (_HTML
            .replace("@@TITLE@@", "三议题全链路离线系统预演与方案A核验看板")
            .replace("@@CSS@@", _CSS)
            .replace("@@BANNER@@", _esc(BANNER_TEXT))
            .replace("@@META@@", meta)
            .replace("@@FILTERS@@", _filters(dataset))
            .replace("@@TOUR@@", _tour_nav(dataset["spotlight_tour"]))
            .replace("@@TOURSCRIPT@@", _tour_script(dataset["spotlight_tour"]))
            .replace("@@TOURNOTES@@", _tour_panel(dataset["spotlight_tour"]))
            .replace("@@SECTIONS@@", "\n".join(html_sections))
            .replace("@@FOOTER@@", footer)
            .replace("@@DATA@@", data_json)
            .replace("@@JS@@", _JS))
    path = Path(output_html_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------- 独立核验
def _verify_trace_path(path, data, panel_index, network_index, questionnaires):
    key = path["key"]
    nodes = path["nodes"]
    types = [node["type"] for node in nodes]
    sample_chain = path["kind"] != "source_to_card"
    if sample_chain:
        _need("visible" in types and "answer" in types and "blind" in types,
              "trace_path_incomplete")
    else:
        _need(types[:1] == ["source"] and "evidence" in types, "trace_static_path_incomplete")
    _need(types == sorted(types, key=lambda name: ["source", "evidence", "card", "visible",
                                                   "answer", "blind", "network", "effect",
                                                   "aggregate"].index(name)),
          "trace_path_order_invalid")
    sources = {(row["topic"], row["source_id"]): row for row in data["evidence"]["sources"]}
    items = {(row["topic"], row["evidence_id"]): row for row in data["evidence"]["items"]}
    cards = {(row["topic"], row["card_id"]): row for row in data["evidence"]["cards"]}
    visible_ids = None
    evidence_id = None
    verified = 0
    for node in nodes:
        kind = node["type"]
        if kind == "source":
            row = sources.get((key["topic_id"], node["id"]))
            _need(row is not None, "trace_source_unknown")
            _need(row["url"] == node["value"]["url"]
                  and row["text_sha256"] == node["value"]["text_sha256"]
                  and row["raw_sha256"] == node["value"]["raw_sha256"], "trace_source_mismatch")
        elif kind == "evidence":
            row = items.get((key["topic_id"], node["id"]))
            _need(row is not None, "trace_evidence_unknown")
            _need(row["source_id"] == node["value"]["source_id"]
                  and row["content_sha256"] == node["value"]["content_sha256"]
                  and row["quote_start"] == node["value"]["quote_start"]
                  and row["quote_end"] == node["value"]["quote_end"], "trace_evidence_mismatch")
            evidence_id = node["id"]
        elif kind == "card":
            row = cards.get((key["topic_id"], node["id"]))
            _need(row is not None, "trace_card_unknown")
            _need(row["evidence_id"] == node["value"]["evidence_id"]
                  and row["source_id"] == node["value"]["source_id"]
                  and row["text_sha256"] == node["value"]["text_sha256"]
                  and row["phase"] == node["value"]["phase"], "trace_card_mismatch")
            if evidence_id is not None:
                _need(row["evidence_id"] == evidence_id, "trace_card_evidence_broken")
        elif kind == "visible":
            ids = node["value"]["card_ids"]
            _need(node["value"]["count"] == len(ids) and node["value"]["sha256"] == _sha(ids),
                  "trace_visible_hash_mismatch")
            _need(node["value"]["world_id"] == key["world_id"], "trace_visible_world_mismatch")
            for card_id in ids:
                _need((key["topic_id"], card_id) in cards, "trace_visible_card_unknown")
            visible_ids = ids
        elif kind == "answer":
            _need(visible_ids is not None, "trace_visible_missing")
            spec = {"topic_id": key["topic_id"], "timepoint": key["timepoint"],
                    "condition_tag": key["condition_tag"], "visible_card_ids": visible_ids}
            output = sr._synthetic_measurement(spec, questionnaires[key["topic_id"]])
            _need(_sha(output) == node["value"]["output_sha256"], "trace_answer_hash_mismatch")
            _need(node["value"]["output_sha256"] == panel_index[node["id"]]["msha"],
                  "trace_answer_panel_mismatch")
            answer = next(item for item in output["answers"]
                          if item["item_id"] == node["value"]["item_id"])
            _need(answer["p_true"] == node["value"]["p_true"]
                  and answer["cited_card_ids"] == node["value"]["cited_card_ids"],
                  "trace_answer_value_mismatch")
            _need(all(cid in visible_ids for cid in answer["cited_card_ids"]),
                  "trace_citation_unseen")
        elif kind == "blind":
            row = panel_index[node["id"]]
            digest = _sha([key["topic_id"], key["timepoint"], key["condition_tag"], node["id"]])
            parts = [int(digest[i], 16) % 3 for i in (12, 13, 14)]
            _need(parts == node["value"]["u_parts"], "trace_u_parts_mismatch")
            _need(abs(100 * sum(parts) / 6 - node["value"]["U"]) <= 1e-9, "trace_u_mismatch")
            _need(abs(row["U"] - node["value"]["U"]) <= 1e-9
                  and abs(row["O"] - node["value"]["O"]) <= 1e-9, "trace_blind_panel_mismatch")
        elif kind == "network":
            record = network_index.get((key["topic_id"], key["block_id"], key["world_id"],
                                        key["arm"], key["timepoint"]))
            _need(record is not None, "trace_network_unknown")
            _need(record["BS"] == node["value"]["BS"] and record["O"] == node["value"]["O"]
                  and record["U"] == node["value"]["U"]
                  and record["valid"] is node["value"]["valid"],
                  "trace_network_mismatch")
        elif kind == "effect":
            endpoint = key["timepoint"]
            comparison = data["effects"]["intervention"][endpoint]["comparisons"]["I4_vs_I0"]
            _need(comparison["effect"] == node["value"]["effect"]
                  and comparison["ci_95"] == node["value"]["ci_95"]
                  and comparison["p_raw"] == node["value"]["p_raw"]
                  and comparison["p_adjusted"] == node["value"]["p_adjusted"],
                  "trace_effect_mismatch")
        elif kind == "aggregate":
            if node["id"].startswith("2x2@"):
                topic_id = node["id"].split("@", 1)[1]
                expected = data["effects"]["exposure"]["per_topic"][topic_id]["contrasts"]
                _need(expected == node["value"]["contrasts"], "trace_aggregate_mismatch")
            elif node["id"].startswith("env@"):
                environment = node["id"].split("@", 1)[1]
                flow = next(row for row in data["info_flow"]
                            if row["environment"] == environment)
                _need(flow["X"] == node["value"]["X"] and flow["Hn"] == node["value"]["Hn"],
                      "trace_env_mismatch")
        verified += 1
    return verified


def _css_rule(css, selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return match.group(1) if match else None


_UI_BLOCK_CHILD_TAGS = ("div", "p", "ul", "ol", "dl", "table", "section", "figure",
                        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote")
_UI_BLOCK_LEVEL_DISPLAYS = ("inline-flex", "flex", "inline-block", "block",
                            "inline-grid", "grid")


def _ui_layout_audit(html):
    """UI 布局防回归审计。

    针对步骤38定位的缺陷建立硬性断言：`.chip` 必须块级/弹性化（否则行内 span 内嵌
    `display:block` 的 small 会被浏览器拆裂、胶囊边框断裂）；静态与动态时间轴都必须
    使用 `.chips` 弹性容器；宽表必须有横向滚动保护；网络回放必须并排网格；顶栏
    只允许一个 sticky 容器，避免 banner 折行遮挡筛选器。
    """
    style = re.search(r"<style>(.*?)</style>", html, flags=re.S)
    _need(style is not None, "ui_style_block_missing")
    css = style.group(1)
    compact = re.sub(r"\s+", "", css)

    chip = _css_rule(css, ".chip")
    _need(chip is not None, "ui_chip_rule_missing")
    display = re.search(r"display\s*:\s*([a-z-]+)", chip)
    _need(display is not None and display.group(1) in _UI_BLOCK_LEVEL_DISPLAYS,
          "ui_chip_display_not_block_level")
    _need(re.search(r"flex-direction\s*:\s*column", chip) is not None,
          "ui_chip_flex_direction_missing")
    _need(re.search(r"\.chip\s+small\s*\{[^}]*display\s*:\s*block", css, flags=re.S) is not None,
          "ui_chip_small_block_missing")

    chips = _css_rule(css, ".chips")
    _need(chips is not None and re.search(r"flex-wrap\s*:\s*wrap", chips) is not None,
          "ui_chips_not_wrapping")
    table_wrap = _css_rule(css, ".table-wrap")
    _need(table_wrap is not None, "ui_table_wrap_rule_missing")
    _need(re.search(r"overflow-x\s*:\s*auto", table_wrap) is not None,
          "ui_table_wrap_scroll_missing")
    net_grid = _css_rule(css, ".net-grid")
    _need(net_grid is not None and "grid-template-columns" in net_grid, "ui_net_grid_rule_missing")
    _need(".filters{position:sticky" not in compact, "ui_filter_double_sticky")
    _need("top:56px" not in compact, "ui_filter_fixed_offset_regression")

    # 行内元素不得包含块级子元素，除非其自身已被声明为弹性/块级盒（如 .chip）
    violations = []
    for match in re.finditer(r"<span\b([^>]*)>", html):
        attrs = match.group(1)
        close = html.find("</span>", match.end())
        _need(close >= 0, "ui_span_unterminated")
        inner = html[match.end():close]
        classes = re.search(r'class="([^"]*)"', attrs)
        names = classes.group(1).split() if classes else []
        if "<small" in inner and "chip" not in names:
            violations.append("span>small:" + " ".join(names)[:60])
        for tag in _UI_BLOCK_CHILD_TAGS:
            if f"<{tag}" in inner:
                violations.append("span>" + tag + ":" + " ".join(names)[:60])
    _need(not violations, "ui_inline_block_conflict")

    timeline_rows = html.count('class="timeline-row"')
    _need(timeline_rows >= 6, "ui_timeline_rows_missing")
    _need(re.search(r'class="timeline-row"[\s\S]{0,800}?class="chips"', html) is not None,
          "ui_timeline_chips_missing")
    net_cards = html.count('class="net-card"')
    _need('class="net-grid"' in html and net_cards >= 6, "ui_network_grid_missing")
    _need('class="topbar" id="topbar"' in html, "ui_topbar_missing")

    js = html[html.rfind("(function(){"):] if "(function(){" in html else ""
    _need(js, "ui_js_missing")
    start = js.find("function renderTimeline(){")
    _need(start >= 0, "ui_timeline_renderer_missing")
    stop = js.find("\n  }", start)
    _need(stop > start, "ui_timeline_renderer_unterminated")
    body = js[start:stop]
    _need('class="timeline-row"' in body and 'class="chips"' in body,
          "ui_timeline_renderer_layout_regression")
    _need("tblWrap(" in js and 'class="table-wrap"' in js, "ui_js_table_wrap_missing")
    net_start = js.find("function renderNetwork(){")
    _need(net_start >= 0, "ui_network_renderer_missing")
    net_stop = js.find("\n  }", net_start)
    _need(net_stop > net_start, "ui_network_renderer_unterminated")
    _need("<svg" not in js[net_start:net_stop], "ui_network_renderer_duplicate_graphics")

    return {
        "chip_display": display.group(1),
        "timeline_rows": timeline_rows,
        "network_cards": net_cards,
        "checks": {
            "ui_chip_display": display.group(1),
            "ui_timeline_rows": timeline_rows,
            "ui_network_cards": net_cards,
            "ui_table_wrap_scroll": True,
            "ui_single_sticky_topbar": True,
            "ui_inline_block_conflicts": 0,
            "ui_timeline_renderer_chips": True,
        },
    }


def verify_dashboard_bundle(dashboard_dir) -> dict:
    """§12.4 独立验收：15+ 条全链追溯、聚合值重算、11 视图、横幅与外链/凭据检查。"""
    directory = Path(dashboard_dir)
    data_path = directory / "dashboard_data.json"
    html_path = directory / "dashboard.html"
    _need(data_path.is_file(), "dataset_file_missing")
    _need(html_path.is_file(), "html_file_missing")
    data = json.loads(data_path.read_text(encoding="utf-8"))
    html = html_path.read_text(encoding="utf-8")
    _need(type(data) is dict and data.get("contract") == DATASET_VERSION, "dataset_contract_invalid")
    body = {key: value for key, value in data.items() if key != "dataset_sha256"}
    _need(data.get("dataset_sha256") == _sha(body), "dataset_hash_mismatch")

    checks = {}
    # 1. 横幅与 11 个视图模块
    _need(data["banner"]["text"] == BANNER_TEXT, "banner_text_changed")
    _need(data["banner"]["persistent"] is True, "banner_not_persistent")
    banner_block = re.search(r'<div class="banner"[^>]*id="top-banner"[^>]*>(.*?)</div>',
                             html, flags=re.S)
    _need(banner_block is not None and BANNER_TEXT in banner_block.group(1),
          "banner_missing_in_html")
    for view_id, title, _desc in VIEWS:
        _need(f'id="{view_id}"' in html and title in html, "view_missing_" + view_id)
        short = view_id.split("-")[1]
        _need(f'id="live-{short}"' in html, "view_live_missing_" + view_id)
    checks["views_present"] = len(VIEWS)
    checks["banner_present"] = True

    # 2. 无外部脚本/样式加载与敏感凭据
    stripped = re.sub(r'<script id="dashboard-data"[^>]*>.*?</script>', "", html, flags=re.S)
    for pattern in _FORBIDDEN_LOAD:
        _need(pattern.search(stripped) is None, "external_resource_load_detected")
    _need(_CREDENTIAL.search(html) is None, "credential_detected")
    _need("http://" not in stripped and "https://" not in stripped,
          "external_url_in_markup")
    checks["no_external_resources"] = True
    checks["no_credentials"] = True

    # 3. 内嵌数据与文件一致
    match = re.search(r'<script id="dashboard-data"[^>]*>(.*?)</script>', html, flags=re.S)
    _need(match is not None, "embedded_data_missing")
    embedded = json.loads(match.group(1))
    _need(_sha({k: v for k, v in embedded.items() if k != "dataset_sha256"})
          == data["dataset_sha256"], "embedded_data_mismatch")
    checks["embedded_data_matches"] = True

    # 4. 面板→网络聚合重算
    panel_index = {row["sid"]: row for row in data["panel"]}
    network_index = {(row["topic_id"], row["block_id"], row["world_id"], row["arm"],
                      row["timepoint"]): row for row in data["network_records"]}
    groups = {}
    for row in data["panel"]:
        groups.setdefault((row["topic"], row["block"], row["world"], row["arm"], row["tp"]),
                          []).append(row)
    _need(len(groups) == len(data["network_records"]), "network_group_count_mismatch")
    for record in data["network_records"]:
        key = (record["topic_id"], record["block_id"], record["world_id"], record["arm"],
               record["timepoint"])
        members = groups[key]
        _need(len(members) == record["observed_agents"], "network_observed_mismatch")
        for metric in ("BS", "Acc", "S", "O", "U"):
            mean = sum(row[metric] for row in members) / len(members)
            _need(abs(mean - record[metric]) <= 1e-9, "network_mean_mismatch")
        _need(sorted(row["sid"] for row in members) == sorted(record["source_blind_sample_ids"]),
              "network_sample_ids_mismatch")
    checks["network_recomputed"] = len(data["network_records"])

    # 5. 2×2 对比与 Holm 校正重算
    for block in data["effects"]["exposure"]["per_block"]:
        if block.get("changes"):
            _need(metrics.exposure_contrasts(block["changes"]) == block["contrasts"],
                  "exposure_contrast_mismatch")
    for topic_id, row in data["effects"]["exposure"]["per_topic"].items():
        if row["pooled_changes"]:
            _need(metrics.exposure_contrasts(row["pooled_changes"]) == row["contrasts"],
                  "exposure_topic_contrast_mismatch")
    for endpoint, node in data["effects"]["intervention"].items():
        if all(value is not None for value in node["p_raw"].values()):
            _need(metrics.holm(dict(node["p_raw"])) == node["p_adjusted"], "holm_mismatch")
    checks["exposure_contrasts_recomputed"] = len(data["effects"]["exposure"]["per_block"])
    checks["holm_recomputed"] = len(data["effects"]["intervention"])

    # 6. 15+ 条全链追溯
    paths = data["trace_paths"]
    _need(len(paths) >= 15, "insufficient_trace_paths")
    questionnaires = data["questionnaires"]
    verified_nodes = 0
    verified_paths = 0
    for path in paths:
        verified_nodes += _verify_trace_path(path, data, panel_index, network_index,
                                            questionnaires)
        verified_paths += 1
    _need(verified_paths >= 15, "trace_replay_insufficient")
    checks["trace_paths_verified"] = verified_paths
    checks["trace_nodes_verified"] = verified_nodes

    # 7. 方案A 重跑比对
    merge = aar.run_scheme_a_merge_and_verify(
        data["lineage_paths"]["template_path"],
        evidence_build=data["lineage_paths"]["evidence_build_dir"],
        live_semantic_path=data["lineage_paths"]["live_semantic_path"])
    overview = data["overview"]["checklist"]
    _need(merge["human_slots_completed"] == overview["human_slots_completed"]
          and merge["items_passed"] == overview["items_passed"]
          and merge["disagreement_count"] == overview["disagreement_count"]
          and merge["human_reviews_completed"] == overview["human_reviews_completed"]
          and merge["all_55_items_passed"] is overview["all_55_items_passed"]
          and merge["release_gates_passed"] is overview["release_gates_passed"],
          "checklist_summary_mismatch")
    _need(len(data["governance"]["disputes"]) == merge["disagreement_count"],
          "dispute_count_mismatch")
    checks["scheme_a_reverified"] = True

    # 8. 诚实门禁与视图数据完整性
    for key, expected in (("is_demo", True), ("actual_model_calls", 0),
                          ("formal_eligible", False), ("approved_for_experiment", False)):
        _need(data["honest_gates"][key] is expected, "honest_gate_" + key)
    _need(data["honest_gates"]["review_origin"] == aar.REVIEW_ORIGIN, "review_origin_mismatch")
    _need(data["honest_gates"]["real_human_ca_signature_verified"] is False,
          "real_human_ca_mismatch")
    required = {
        "view-01": data["overview"]["matrix"],
        "view-02": data["timeline"],
        "view-03": data["network"],
        "view-04": data["info_flow"],
        "view-05": data["series"],
        "view-06": data["interventions"],
        "view-07": data["trace_paths"],
        "view-08": data["effects"]["intervention"],
        "view-09": data["calibration"]["overall"]["bin_labels"],
        "view-10": data["coverage"],
        "view-11": data["governance"]["disputes"],
    }
    for view_id, value in required.items():
        _need(bool(value), "view_data_empty_" + view_id)
    checks["view_data_complete"] = len(required)

    # 9. 六轮真实组织侧＋原句对照：清单重述与 I4 来源一致性
    live = data.get("live_delivery") or {}
    _need(type(live) is dict and "available" in live, "live_delivery_block_missing")
    if live.get("available"):
        recomputed = lcd.summarize_live_delivery(Path(__file__).resolve().parents[1])
        _need(recomputed["available"] is True, "live_manifest_not_verified")
        _need(recomputed["manifest_sha256"] == live["manifest_sha256"],
              "live_manifest_mismatch")
        _need(recomputed["case_count"] == live["case_count"] == 6, "live_case_count_mismatch")
        _need(all(all(row["checks"].values()) for row in recomputed["cases"]),
              "live_pair_checks_failed")
        _need(all(all(row["checks"].values()) for row in live["cases"]),
              "live_pair_checks_not_embedded")
        total = sum(float(row["charged_cny"]) for row in recomputed["cases"])
        _need("%.8f" % total == recomputed["charged_cny"] == live["charged_cny"],
              "live_charge_mismatch")
        _need(all(item["content_origin"] == "live_model_organized_output"
                  for item in data["interventions"] if item["arm"] == "I4"),
              "live_i4_origin_mismatch")
        _need(all(item["content_origin"] == "deterministic_exact_source_sentences"
                  for item in data["interventions"] if item["arm"] == "I5"),
              "live_i5_origin_mismatch")
        checks["live_delivery_verified"] = live["case_count"]
        checks["live_delivery_charged_cny"] = live["charged_cny"]
    else:
        checks["live_delivery_available"] = False

    # 9c. 三议题真实实证矩阵：重算并与嵌入数据逐项一致
    matrix = data.get("live_micro_matrix") or {}
    _need(type(matrix) is dict and "available" in matrix, "live_micro_matrix_block_missing")
    if matrix.get("available"):
        again_matrix = lmp.summarize_three_topic_matrix(Path(__file__).resolve().parents[1])
        _need(again_matrix.get("available") is True, "live_micro_matrix_not_verified")
        for key in ("matrix_sha256", "totals", "topics", "descriptive_by_topic_arm", "honest_gates"):
            _need(again_matrix.get(key) == matrix.get(key), "live_micro_matrix_mismatch_" + key)
        _need([s["comparison"] for s in again_matrix["topic_summaries"]]
              == [s["comparison"] for s in matrix["topic_summaries"]], "live_micro_matrix_mismatch_comparison")
        _need(_MATRIX_TITLE in html, "live_micro_matrix_section_missing_in_html")
        _need(html.count('class="mini-traj"') >= 3, "live_micro_matrix_svg_missing")
        checks["live_micro_matrix_topics"] = len(matrix["topics"])
        checks["live_micro_matrix_calls"] = matrix["totals"]["final_run_model_calls"]
    else:
        checks["live_micro_matrix_available"] = False

    # 9b. 真实微型实证：清单离线重验并与嵌入数据逐项一致
    micro = data.get("live_micro_pilot") or {}
    _need(type(micro) is dict and "available" in micro, "live_micro_pilot_block_missing")
    if micro.get("available"):
        again = lmp.summarize_live_micro_pilot(Path(__file__).resolve().parents[1])
        _need(again.get("available") is True, "live_micro_pilot_not_verified")
        for key in ("manifest_sha256", "run_id", "charged_cny", "actual_model_calls", "comparison",
                    "panel_sha256", "registry_sha256", "step_spend", "superseded_runs"):
            _need(again.get(key) == micro.get(key), "live_micro_pilot_mismatch_" + key)
        _need(_MICRO_TITLE in html, "live_micro_pilot_section_missing_in_html")
        _need(micro["honest_gates"]["formal_eligible"] is False, "live_micro_pilot_formal_gate")
        checks["live_micro_pilot_verified"] = len(micro["comparison"])
        checks["live_micro_pilot_charged_cny"] = micro["charged_cny"]
    else:
        checks["live_micro_pilot_available"] = False

    # 9d. §5.4 封闭情境真实实证（步骤42）：清单离线重验并与嵌入数据逐项一致
    closed = data.get("closed_world_pilot") or {}
    _need(type(closed) is dict and "available" in closed, "closed_world_block_missing")
    _need(closed.get("is_synthetic_closed_world") is True, "closed_world_synthetic_flag_missing")
    if closed.get("available"):
        again_closed = cwp.summarize_closed_world_live_pilot(Path(__file__).resolve().parents[1])
        _need(again_closed.get("available") is True, "closed_world_not_verified")
        for key in ("manifest_sha256", "run_id", "charged_cny", "actual_model_calls", "comparison", "item_level",
                    "descriptive_by_arm", "panel_sha256", "registry_sha256", "honest_gates", "subjects",
                    "materials_sha256", "step_spend"):
            _need(again_closed.get(key) == closed.get(key), "closed_world_mismatch_" + key)
        _need(closed["honest_gates"]["formal_eligible"] is False
              and closed["honest_gates"]["is_synthetic_closed_world"] is True
              and closed["honest_gates"]["protocol_basis"] == cwp.PROTOCOL_BASIS, "closed_world_honest_gate")
        _need(_SYNTH_TITLE in html and 'id="spot-synth"' in html, "closed_world_section_missing_in_html")
        for subject in closed["subjects"]:
            _need(_esc(subject["speech"]) in html, "closed_world_speech_missing_in_html")
        checks["closed_world_verified"] = len(closed["comparison"])
        checks["closed_world_calls"] = closed["actual_model_calls"]
    else:
        checks["closed_world_available"] = False

    # 9e. 双轨对比与路演导览：由嵌入的矩阵/封闭情境摘要确定性重算
    dual = data.get("dual_track") or {}
    _need(type(dual) is dict and "available" in dual, "dual_track_block_missing")
    _need(_dual_track(matrix, closed) == dual, "dual_track_mismatch")
    _need(_DUAL_TITLE in html and 'id="spot-dualtrack"' in html, "dual_track_section_missing_in_html")
    if dual.get("available"):
        _need(html.count('class="dual-traj"') == len(_DUAL_METRICS), "dual_track_svg_missing")
    tour = data.get("spotlight_tour") or {}
    _need(type(tour) is dict and len(tour.get("steps") or []) == 7, "spotlight_tour_steps_invalid")
    expected_tour = _spotlight_tour(data["overview"], len(data["trace_paths"]), matrix, closed, dual,
                                    data["overview"]["billing"])
    _need(expected_tour == tour, "spotlight_tour_mismatch")
    _need('id="spotlight-tour"' in html and 'id="tour-notes"' in html and 'id="tour-script"' in html,
          "spotlight_tour_markup_missing")
    for step in tour["steps"]:
        _need(f'id="{step["target"]}"' in html, "spotlight_target_missing_" + step["key"])
        if step.get("secondary_target"):
            _need(f'id="{step["secondary_target"]}"' in html, "spotlight_secondary_missing_" + step["key"])
        _need(f'data-step="{step["step"] - 1}"' in html, "spotlight_button_missing_" + step["key"])
    _need("function initTour(){" in html and "scrollIntoView" in html, "spotlight_tour_script_missing")
    checks["spotlight_tour_steps"] = len(tour["steps"])
    checks["dual_track_available"] = bool(dual.get("available"))

    # 10. UI 布局防回归（.chip 撕裂、时间轴容器、宽表滚动、网络网格、顶栏衔接）
    ui = _ui_layout_audit(html)
    checks.update(ui["checks"])

    return {
        "contract": VERIFICATION_VERSION,
        "passed": True,
        "dataset_sha256": data["dataset_sha256"],
        "views": len(VIEWS),
        "trace_paths_verified": verified_paths,
        "trace_nodes_verified": verified_nodes,
        "network_records_recomputed": len(data["network_records"]),
        "panel_samples": len(data["panel"]),
        "checks": checks,
        "live_micro_matrix": {
            "available": bool(matrix.get("available")),
            "matrix_sha256": matrix.get("matrix_sha256"),
            "totals": matrix.get("totals"),
        },
        "closed_world_pilot": {
            "available": bool(closed.get("available")),
            "is_synthetic_closed_world": True,
            "protocol_basis": cwp.PROTOCOL_BASIS,
            "manifest_sha256": closed.get("manifest_sha256"),
            "run_id": closed.get("run_id"),
            "charged_cny": closed.get("charged_cny"),
            "actual_model_calls": closed.get("actual_model_calls"),
        },
        "dual_track": {"available": bool(dual.get("available")), "headline": dual.get("headline")},
        "spotlight_tour": {"steps": len(tour["steps"]), "targets": [st["target"] for st in tour["steps"]]},
        "live_micro_pilot": {
            "available": bool(micro.get("available")),
            "manifest_sha256": micro.get("manifest_sha256"),
            "run_id": micro.get("run_id"),
            "charged_cny": micro.get("charged_cny"),
            "step_charged_cny": (micro.get("step_spend") or {}).get("step_charged_cny"),
        },
        "live_delivery": {
            "available": bool(live.get("available")),
            "case_count": live.get("case_count", 0),
            "manifest_sha256": live.get("manifest_sha256"),
            "charged_cny": live.get("charged_cny"),
            "new_live_calls": live.get("new_live_calls"),
        },
        "ui_layout": {
            "chip_display": ui["chip_display"],
            "timeline_rows": ui["timeline_rows"],
            "network_cards": ui["network_cards"],
            "topbar_single_sticky": True,
            "inline_block_conflicts": 0,
        },
        "banner": BANNER_TEXT,
        "honest_gates": data["honest_gates"],
        "is_demo": True,
        "actual_model_calls": 0,
        "formal_eligible": False,
        "new_cost_cny": "0",
        "verification_scope": (
            "本地只读派生数据与单文件HTML的结构、数值重算、15+全链追溯与方案A重跑比对；"
            "不是真实模型实验结果，也不认证真人身份。"),
    }
