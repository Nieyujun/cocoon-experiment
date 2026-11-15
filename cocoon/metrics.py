"""方案第10/11节的评分和固定议题条件统计；仅使用标准库。"""

import math
import random


def _finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是非 bool 的有限数值")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数值")
    return number


def _validate_stance(value, name, allowed=(-1, 0, 1)):
    if type(value) is not int or value not in allowed:
        raise ValueError(f"{name} 必须是 {allowed} 中的整数，不能是 bool")


def score_answers(answers: list[dict], gold: dict[str, int]) -> dict:
    """固定 gold 分母；按 p_true >= 0.5 评分，choice 只用于标记不一致。

    choice 可省略，否则须为 bool 或整数0/1。缺题不填概率；此时三个
    主分数为 None，边界仍以完整 gold 为分母。计数仅描述已观察回答。
    """
    if not isinstance(answers, list) or not isinstance(gold, dict) or not gold:
        raise ValueError("answers 必须是列表，gold 必须是非空字典")
    for item_id, truth in gold.items():
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("gold 的题目 ID 必须是非空字符串")
        if type(truth) is not int or truth not in (0, 1):
            raise ValueError("gold 真值必须是整数0/1，不能是 bool")

    seen = set()
    errors = []
    correct = confident_wrong = uncertain = inconsistent = 0
    for answer in answers:
        if not isinstance(answer, dict):
            raise ValueError("每份回答必须是字典")
        item_id = answer.get("item_id")
        if not isinstance(item_id, str) or item_id not in gold:
            raise ValueError("回答包含未知或无效的题目 ID")
        if item_id in seen:
            raise ValueError(f"重复题目 ID：{item_id}")
        seen.add(item_id)
        probability = _finite_number(answer.get("p_true"), "p_true")
        if not 0 <= probability <= 1:
            raise ValueError("p_true 必须在0到1之间")
        predicted = probability >= 0.5
        if "choice" in answer:
            choice = answer["choice"]
            if type(choice) not in (bool, int) or choice not in (0, 1):
                raise ValueError("choice 必须是 bool 或整数0/1")
            inconsistent += bool(choice) != predicted
        is_correct = predicted == gold[item_id]
        confidence = probability if predicted else 1 - probability
        correct += is_correct
        confident_wrong += not is_correct and confidence >= 0.8
        uncertain += probability == 0.5
        errors.append((probability - gold[item_id]) ** 2)

    expected_n = len(gold)
    missing_ids = [item_id for item_id in gold if item_id not in seen]
    complete = not missing_ids
    observed_error = math.fsum(errors)
    lower = observed_error / expected_n
    return {
        "complete": complete,
        "expected_n": expected_n,
        "observed_n": len(seen),
        "missing_ids": missing_ids,
        "BS": lower if complete else None,
        "Acc": 100 * correct / expected_n if complete else None,
        "S": 100 * confident_wrong / expected_n if complete else None,
        "BS_lower": lower,
        "BS_upper": (observed_error + len(missing_ids)) / expected_n,
        "uncertain_count": uncertain,
        "choice_inconsistencies": inconsistent,
    }


def coverage_score(dimensions: list[dict]) -> dict:
    """六维固定分母、百分数；识别率只计未掌握但识别出缺口的维度。"""
    if not isinstance(dimensions, list) or len(dimensions) != 6:
        raise ValueError("必须提供固定的六个证据维度")
    mastered = recognized = 0
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            raise ValueError("每个证据维度必须是字典")
        if any(type(dimension.get(key)) is not bool
               for key in ("mastered", "recognized_gap")):
            raise ValueError("mastered 和 recognized_gap 必须都是 bool")
        mastered += dimension["mastered"]
        recognized += not dimension["mastered"] and dimension["recognized_gap"]
    return {
        "O": 100 * (6 - mastered - recognized) / 6,
        "mastered_rate": 100 * mastered / 6,
        "recognized_gap_rate": 100 * recognized / 6,
    }


def understanding_score(parts: list[int]) -> float:
    """复述、论据、适用边界各0到2分，转换为百分制。"""
    if not isinstance(parts, list) or len(parts) != 3:
        raise ValueError("理解评分必须恰有三个分项")
    if any(type(part) is not int or not 0 <= part <= 2 for part in parts):
        raise ValueError("各分项必须是0到2的整数，不能是 bool")
    return 100 * sum(parts) / 6


def exposure_metrics(stances: list[int], initial_stance: int) -> dict:
    """曝光编码为-1/0/1，初始立场仅±1；熵固定以 log2(3) 归一化。

    X 为相反立场曝光百分数；neutral 保留在曝光分母中，但绝不计入异见。
    """
    if not isinstance(stances, list):
        raise ValueError("stances 必须是列表")
    _validate_stance(initial_stance, "initial_stance", allowed=(-1, 1))
    counts = {-1: 0, 0: 0, 1: 0}
    for stance in stances:
        _validate_stance(stance, "曝光立场")
        counts[stance] += 1
    if not stances:
        return {"Hn": None, "X": None}
    n = len(stances)
    entropy = -math.fsum((count / n) * math.log2(count / n)
                         for count in counts.values() if count)
    return {
        "Hn": entropy / math.log2(3),
        "X": 100 * counts[-initial_stance] / n,
    }


def interaction_rate(edges: list[tuple[str, str]], stances: dict) -> float | None:
    """初始群体只允许±1；按事件计数（不去重），返回跨群体互动百分数。"""
    if not isinstance(edges, list) or not isinstance(stances, dict):
        raise ValueError("edges 必须是列表，stances 必须是字典")
    for agent_id, stance in stances.items():
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("互动主体 ID 必须是非空字符串")
        _validate_stance(stance, "初始群体立场", allowed=(-1, 1))
    cross_group = 0
    for edge in edges:
        if not isinstance(edge, tuple) or len(edge) != 2:
            raise ValueError("每次互动必须是两个主体 ID 构成的元组")
        if any(not isinstance(agent_id, str) or agent_id not in stances
               for agent_id in edge):
            raise ValueError("互动包含未知或无效主体 ID")
        cross_group += stances[edge[0]] != stances[edge[1]]
    return 100 * cross_group / len(edges) if edges else None


def exposure_contrasts(changes: dict) -> dict:
    """changes 的 A/B/C/D 为各曝光环境的 T1-T0 网络变化量。"""
    if not isinstance(changes, dict) or set(changes) != {"A", "B", "C", "D"}:
        raise ValueError("changes 必须且只能包含 A、B、C、D")
    a, b, c, d = (_finite_number(changes[key], key) for key in "ABCD")
    return {
        "homogeneous": ((c + d) - (a + b)) / 2,
        "misinformation": ((b + d) - (a + c)) / 2,
        "interaction": (d - c) - (b - a),
    }


def holm(p_values: dict[str, float]) -> dict:
    """返回保持输入键顺序的 Holm 校正 p 值；排序后累积最大保证单调。"""
    if not isinstance(p_values, dict):
        raise ValueError("p_values 必须是字典")
    validated = {}
    for name, value in p_values.items():
        if not isinstance(name, str) or not name:
            raise ValueError("比较名称必须是非空字符串")
        value = _finite_number(value, f"p_values[{name}]")
        if not 0 <= value <= 1:
            raise ValueError("p 值必须在0到1之间")
        validated[name] = value
    adjusted = {}
    running_max = 0.0
    for rank, (name, value) in enumerate(sorted(validated.items(), key=lambda item: item[1])):
        running_max = max(running_max, min(1.0, (len(validated) - rank) * value))
        adjusted[name] = running_max
    return {name: adjusted[name] for name in p_values}


def _percentile(sorted_values, probability):
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def paired_summary(records: list[dict], target_topics: list[str],
                   bootstrap_reps=10000, seed=0) -> dict:
    """I4 对 I0/I5 的配对网络统计，效应为对照变化减处理变化。

    返回 comparisons[I4_vs_I0/I4_vs_I5]，含 effect、ci_95、per_topic、
    n、count_missing、coverage、estimable 和空 p 值。per_topic 的分母
    expected_n 是该议题所有输入臂中出现过的不同区组数；coverage 为0到1
    的完整配对比例，无已知区组时为 None。完全未提交的区组无法推知。

    target_topics 须非空且唯一，固定等权；不属于目标议题的记录不入估计。
    每个议题/区组/臂只能一条记录；block_id 为非空字符串或非 bool 整数。
    valid=False、T1/T3 缺键或为 None 都保留在分母，但不构成有效配对。
    任意记录的 is_demo=True 均拒绝，包括非目标议题。所有非空 T1/T3
    必须是有限的0到1 BS 数值（非 bool）。有效配对的对照臂与 I4 必须
    共享 T1，绝对差超过1e-12则抛出 ValueError，不将其算入配对结果。
    """
    if not isinstance(records, list):
        raise ValueError("records 必须是列表")
    if (not isinstance(target_topics, list) or not target_topics
            or any(not isinstance(topic, str) or not topic for topic in target_topics)):
        raise ValueError("target_topics 必须是非空的议题字符串列表")
    if len(set(target_topics)) != len(target_topics):
        raise ValueError("target_topics 不能重复")
    if type(bootstrap_reps) is not int or bootstrap_reps <= 0:
        raise ValueError("bootstrap_reps 必须是正整数，不能是 bool")
    if type(seed) is not int:
        raise ValueError("seed 必须是整数，不能是 bool")

    networks = {}
    blocks = {topic: set() for topic in target_topics}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("每条网络记录必须是字典")
        if type(record.get("is_demo", False)) is not bool:
            raise ValueError("is_demo 必须是 bool")
        if record.get("is_demo", False):
            raise ValueError("演示数据 is_demo=True 不能用于正式分析")
        topic, block, arm = (record.get(key) for key in ("topic_id", "block_id", "arm"))
        if not isinstance(topic, str) or not topic:
            raise ValueError("topic_id 必须是非空字符串")
        if not ((isinstance(block, str) and block) or type(block) is int):
            raise ValueError("block_id 必须是非空字符串或非 bool 整数")
        if not isinstance(arm, str) or arm not in {"I0", "I1", "I2", "I3", "I4", "I5"}:
            raise ValueError("arm 必须是 I0 到 I5")
        if type(record.get("valid")) is not bool:
            raise ValueError("valid 必须是 bool")
        key = (topic, block, arm)
        if key in networks:
            raise ValueError("同一议题、区组和臂出现重复网络记录")
        levels = {}
        for timepoint in ("T1", "T3"):
            value = record.get(timepoint)
            if value is not None:
                value = _finite_number(value, f"BS {timepoint}")
                if not 0 <= value <= 1:
                    raise ValueError(f"BS {timepoint} 必须在0到1之间")
            levels[timepoint] = value
        if record["valid"] and all(value is not None for value in levels.values()):
            levels["change"] = _finite_number(levels["T3"] - levels["T1"], "网络变化量")
            networks[key] = levels
        else:
            networks[key] = None
        if topic in blocks:
            blocks[topic].add(block)

    comparisons = {}
    for control in ("I0", "I5"):
        per_topic = {}
        paired_effects = {}
        for topic in target_topics:
            effects, control_levels, treatment_levels = [], [], []
            for block in sorted(blocks[topic], key=lambda value: (isinstance(value, str), value)):
                control_row = networks.get((topic, block, control))
                treatment_row = networks.get((topic, block, "I4"))
                if control_row is None or treatment_row is None:
                    continue
                if abs(control_row["T1"] - treatment_row["T1"]) > 1e-12:
                    raise ValueError(f"议题 {topic}、区组 {block} 的 {control} 与 I4 "
                                     "必须共享 T1（绝对差不得超过1e-12）")
                effects.append(_finite_number(control_row["change"] - treatment_row["change"],
                                              "配对效应"))
                control_levels.append(control_row)
                treatment_levels.append(treatment_row)
            n, expected_n = len(effects), len(blocks[topic])
            per_topic[topic] = {
                "n": n,
                "expected_n": expected_n,
                "count_missing": expected_n - n,
                "coverage": n / expected_n if expected_n else None,
                "effect": math.fsum(effects) / n if n else None,
            }
            for name, rows in (("control", control_levels), ("treatment", treatment_levels)):
                per_topic[topic][name] = {
                    key: math.fsum(row[key] / n for row in rows)
                    for key in ("T1", "T3", "change")
                } if n else None
            paired_effects[topic] = effects

        missing_topics = [topic for topic in target_topics if not paired_effects[topic]]
        n = sum(row["n"] for row in per_topic.values())
        expected_n = sum(row["expected_n"] for row in per_topic.values())
        comparison = {
            "treatment_arm": "I4",
            "control_arm": control,
            "estimable": not missing_topics,
            "effect": None,
            "ci_95": None,
            "control": None,
            "treatment": None,
            "n": n,
            "expected_n": expected_n,
            "count_missing": expected_n - n,
            "coverage": n / expected_n if expected_n else None,
            "per_topic": per_topic,
            "missing_topics": missing_topics,
            "reason": "目标议题无有效配对，不能缩减固定议题集合" if missing_topics else None,
            "p_raw": None,
            "p_adjusted": None,
        }
        if not missing_topics:
            topic_n = len(target_topics)
            comparison["effect"] = math.fsum(row["effect"] / topic_n
                                               for row in per_topic.values())
            for name in ("control", "treatment"):
                comparison[name] = {
                    key: math.fsum(row[name][key] / topic_n for row in per_topic.values())
                    for key in ("T1", "T3", "change")
                }
            rng = random.Random(seed)
            bootstrap = []
            strata = [paired_effects[topic] for topic in sorted(target_topics)]
            for _ in range(bootstrap_reps):
                # 每个值来自一个完整区组配对；重采样该差值等价于同时抽取两臂。
                bootstrap.append(math.fsum(
                    math.fsum(rng.choice(values) / len(values) for _ in values) / topic_n
                    for values in strata
                ))
            bootstrap.sort()
            comparison["ci_95"] = [_percentile(bootstrap, 0.025), _percentile(bootstrap, 0.975)]
        comparisons[f"I4_vs_{control}"] = comparison

    return {
        "target_topics": list(target_topics),
        "comparisons": comparisons,
        "bootstrap_reps": bootstrap_reps,
        "seed": seed,
        "ci_method": "固定议题内完整配对区组 bootstrap，95%线性插值百分位区间",
        "p_value_note": "未提供实际槽位随机分配 allocation 及其规则，不能计算随机化 p 值；"
                        "不以 sign-flip 代替实际分配，两项比较的原始及 Holm 校正 p 值均为 None。",
        "note": "方案中的全部三个议题是固定集合；仅对给定目标议题等权作条件推断，"
                "不重采样议题，不向议题总体泛化。coverage 为0到1的完整配对比例，"
                "以已出现区组为分母；完全未提交的区组无法从这些记录推知。"
                "缺少预定区组清单时，不能将该覆盖率正式解读为全实验网络覆盖率。",
    }
