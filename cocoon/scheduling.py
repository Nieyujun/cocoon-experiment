"""仅用标准库的实验调度工具，不运行模型、不产生研究结果。

make_fixture_cards 仅生成内容不真实的 synthetic 工程夹具；其准确性只指
是否符合卡片内的封闭虚构交通参数。调用方主程序必须显式标记模拟运行，
不得将夹具、调度日志或流程测试冒充真实材料、认知观测或研究结果。
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import random
import re
from urllib.parse import unquote


__all__ = [
    "make_population", "make_network", "allocate", "make_fixture_cards",
    "schedule_cards", "public_card",
]

# 顺序为同初始立场、异初始立场、中性。每三轮各类别出现两次错误，
# 且包含同初始立场零错误的轮次；种子仅轮转冻结模式，不按结果调整。
_ERROR_PATTERNS = ((1, 1, 0), (0, 1, 1), (1, 0, 1))
_PUBLIC_FIELDS = (
    "card_id", "topic_id", "text", "source_family_id", "source_url",
    "transformation_type",
)
_LABEL_TOKEN = re.compile(
    r"(?:^|[^a-z0-9])(?:stance|is_accurate|label|labels|analysis|justification|"
    r"answer|answers|true|false|truth|accurate|inaccurate|correct|incorrect|"
    r"fake|real|gold|supports|refutes)(?:$|[^a-z0-9])|"
    r"答案|真假|金标准|正确|错误|真消息|假消息|真目录|假目录|(?:^|[/\\])(?:真|假)(?:$|[/\\])",
    re.IGNORECASE,
)


def _rng(seed: int) -> random.Random:
    if type(seed) is not int:
        raise ValueError("seed 必须是整数")
    return random.Random(seed)


def _check_population_size(n: int) -> None:
    if type(n) is not int or n < 8 or n % 4:
        raise ValueError("人口数必须满足 n >= 8 且 n % 4 == 0")


def make_population(n: int, seed: int) -> list[dict]:
    """生成 S01 起的画像，两阵营共享完全相同的偏好联合分布。

    每个偏好维度的两个取值在各阵营内等量；只描述阅读和背景参数，
    不含事实答案、必然相信某类消息等预设结果。
    """
    _check_population_size(n)
    rng = _rng(seed)
    dimensions = {
        "selection_preference": ("主题连续", "主题多样"),
        "source_sensitivity": ("来源简介", "来源详述"),
        "memory_span": (4, 8),
        "background": ("日常通勤", "周末出行"),
    }
    profiles = [{} for _ in range(n // 2)]
    for key, choices in dimensions.items():
        values = list(choices) * (n // 4)
        rng.shuffle(values)
        for profile, value in zip(profiles, values):
            profile[key] = value
    population = [
        {"initial_stance": stance, "preferences": deepcopy(profile)}
        for stance in (1, -1) for profile in profiles
    ]
    rng.shuffle(population)
    for index, agent in enumerate(population, 1):
        agent["agent_id"] = f"S{index:02d}"
    return population


def make_network(agents: list, seed: int) -> list[dict]:
    """生成有向关注边：每人关注两个同初始立场和两个异初始立场的人。"""
    if not isinstance(agents, list):
        raise ValueError("agents 必须是画像列表")
    _check_population_size(len(agents))
    rng = _rng(seed)
    groups = {1: [], -1: []}
    ids = set()
    for agent in agents:
        if not isinstance(agent, dict):
            raise ValueError("每个画像必须是字典")
        agent_id = agent.get("agent_id")
        stance = agent.get("initial_stance")
        if not isinstance(agent_id, str) or not agent_id or agent_id in ids:
            raise ValueError("agent_id 必须是非空且唯一的字符串")
        if type(stance) is not int or stance not in (-1, 1):
            raise ValueError("initial_stance 必须为整数 -1 或 1")
        ids.add(agent_id)
        groups[stance].append(agent_id)
    if len(groups[1]) != len(groups[-1]):
        raise ValueError("两个初始立场的人数必须各半")
    edges = []
    for agent in agents:
        agent_id, stance = agent["agent_id"], agent["initial_stance"]
        same = [other for other in groups[stance] if other != agent_id]
        followees = rng.sample(same, 2) + rng.sample(groups[-stance], 2)
        rng.shuffle(followees)
        edges.extend(
            {"follower_id": agent_id, "followee_id": other}
            for other in followees
        )
    return edges


def allocate(arms: list[str], seed: int) -> list[dict]:
    """均匀打乱互不重复的臂，分配到从零开始的槽及互异响应随机种子。"""
    if not isinstance(arms, list) or not arms:
        raise ValueError("arms 必须是非空列表")
    if any(not isinstance(arm, str) or not arm for arm in arms):
        raise ValueError("每个臂必须是非空字符串")
    if len(set(arms)) != len(arms):
        raise ValueError("干预臂名称不得重复")
    rng = _rng(seed)
    shuffled = list(arms)
    rng.shuffle(shuffled)
    response_seeds = rng.sample(range(2**32), len(arms))
    return [
        {"arm": arm, "slot": slot, "response_seed": response_seeds[slot]}
        for slot, arm in enumerate(shuffled)
    ]


def _chinese_number(number: int) -> str:
    digits = "零一二三四五六七八九"
    if number < 10:
        return digits[number]
    tens, units = divmod(number, 10)
    return (digits[tens] if tens > 1 else "") + "十" + (digits[units] if units else "")


def make_fixture_cards(topic_id="SYNTHETIC", phase="exposure") -> list[dict]:
    """生成曝光 72 张或保持 36 张合成工程夹具，绝非真实研究数据。

    同一虚构场景的三个立场版本共享来源族；卡片与来源标识不编码真假。
    此接口不生成干预证据包，也不核验现实事实。
    """
    if not isinstance(topic_id, str) or not topic_id.strip():
        raise ValueError("topic_id 必须是非空字符串")
    if phase not in ("exposure", "retention"):
        raise ValueError("夹具 phase 仅支持 exposure 或 retention")
    accurate_count, inaccurate_count = (16, 8) if phase == "exposure" else (8, 4)
    identity = sha256(f"{topic_id}\0{phase}".encode("utf-8")).hexdigest()
    rng = random.Random(int(identity, 16))
    entries = []
    for stance in (-1, 0, 1):
        labels = [True] * accurate_count + [False] * inaccurate_count
        rng.shuffle(labels)
        entries.extend((stance, case, label) for case, label in enumerate(labels))
    rng.shuffle(entries)
    phase_name = "曝光" if phase == "exposure" else "保持"
    perspectives = {
        1: "本条倾向在沙盒内增加班次，重视等候体验。",
        -1: "本条倾向在沙盒内维持班次，重视车辆安排。",
        0: "本条仅描述沙盒内的班次，不主张改变安排。",
    }
    cards = []
    for index, (stance, case, is_accurate) in enumerate(entries):
        seats = (4, 6, 8)[case % 3]
        departures = 2 + (case // 3) % 3
        claimed_total = seats * departures + (0 if is_accurate else 2)
        text = (
            "【合成工程夹具，内容不真实】这里是封闭虚构的纸环交通沙盒，"
            "线路、车辆和数值全部由程序编造，与现实无关，不是研究数据。"
            f"{phase_name}{_chinese_number(case + 1)}号场景只讨论青环线在两座纸站间的固定班次，"
            "不接入外部资料。"
            f"设定每班有{_chinese_number(seats)}个座位，每个观察周期发车{_chinese_number(departures)}班，"
            "车辆彼此独立，全部座位均可使用且不加班。"
            f"本条记录声称该周期合计提供{_chinese_number(claimed_total)}个座位。"
            f"{perspectives[stance]}"
            "材料只供测试配额、顺序、重复与字段隔离，不反映真实观察，"
            "也不代表任何人的认识或实际行为。"
        )
        card_id = "C" + sha256(f"{identity}/card/{index}".encode()).hexdigest()[:24]
        family_id = "F" + sha256(f"{identity}/case/{case}".encode()).hexdigest()[:24]
        cards.append({
            "card_id": card_id,
            "topic_id": topic_id,
            "phase": phase,
            "stance": stance,
            "is_accurate": is_accurate,
            "text": text,
            "source_family_id": family_id,
            "source_url": f"synthetic://fixture/{identity[:16]}/{phase}/{card_id}",
            "transformation_type": "synthetic",
            "is_demo": True,
        })
    return cards


def schedule_cards(
    cards: list, initial_stance: int, environment: str, rounds: int,
    seed: int, max_repeats=2,
) -> list[list[dict]]:
    """先冻结整段计划并校验六类库存，再分配卡片的独立深拷贝。

    A/B 的同、异、中性配额为 2/2/2，C/D 为 4/1/1；A/C 无错误，
    B/D 每轮两张错误。B/D 每三个连续轮次各类别累计两张错误，
    六个位置各出现一次错误；不足三轮的尾部差额不超过一张。
    库存不足指不能满足该 seed 的冻结联合分层表，不通过改变错误
    分布或 seed 来迁就库存。同卡同轮不重复，每次调用最多使用两次，
    max_repeats 可收紧为一次。阶段间重复预算由独立调用管理。
    """
    if not isinstance(cards, list):
        raise ValueError("cards 必须是卡片列表")
    if type(initial_stance) is not int or initial_stance not in (-1, 1):
        raise ValueError("initial_stance 必须为整数 -1 或 1")
    if not isinstance(environment, str) or environment not in ("A", "B", "C", "D"):
        raise ValueError("environment 必须为 A、B、C 或 D")
    if type(rounds) is not int or rounds < 0:
        raise ValueError("rounds 必须是非负整数")
    if type(max_repeats) is not int or max_repeats not in (1, 2):
        raise ValueError("max_repeats 只能为 1 或 2")
    rng = _rng(seed)
    stances = (initial_stance, -initial_stance, 0)
    pools = {(stance, accurate): [] for stance in stances for accurate in (True, False)}
    seen = set()
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError("每张卡必须是字典")
        card_id, stance, accurate = card.get("card_id"), card.get("stance"), card.get("is_accurate")
        if not isinstance(card_id, str) or not card_id or card_id in seen:
            raise ValueError("card_id 必须是非空且唯一的字符串")
        if type(stance) is not int or stance not in (-1, 0, 1):
            raise ValueError("卡片 stance 必须为整数 -1、0 或 1")
        if type(accurate) is not bool:
            raise ValueError("卡片 is_accurate 必须是 bool")
        seen.add(card_id)
        pools[stance, accurate].append(card)

    quotas = (2, 2, 2) if environment in ("A", "B") else (4, 1, 1)
    position_order = list(range(6))
    rng.shuffle(position_order)
    plan = []
    for round_index in range(rounds):
        errors = _ERROR_PATTERNS[(round_index + seed) % 3] if environment in ("B", "D") else (0, 0, 0)
        wrong, right = [], []
        for stance, quota, error_count in zip(stances, quotas, errors):
            wrong.extend([(stance, False)] * error_count)
            right.extend([(stance, True)] * (quota - error_count))
        rng.shuffle(wrong)
        rng.shuffle(right)
        error_positions = (
            {position_order[round_index % 3], position_order[round_index % 3 + 3]}
            if wrong else set()
        )
        wrong_iter, right_iter = iter(wrong), iter(right)
        plan.append([
            next(wrong_iter) if position in error_positions else next(right_iter)
            for position in range(6)
        ])

    demands = [Counter(row) for row in plan]
    deficits = []
    for key, pool in pools.items():
        total = sum(demand[key] for demand in demands)
        peak = max((demand[key] for demand in demands), default=0)
        if total > len(pool) * max_repeats or peak > len(pool):
            deficits.append(
                f"stance={key[0]}, is_accurate={key[1]}: "
                f"库存 {len(pool)}，总需求 {total}，单轮需求峰值 {peak}，重复上限 {max_repeats}"
            )
    if deficits:
        raise ValueError("冻结排程库存不足：" + "；".join(deficits))

    # 同类卡的资格、重复上限完全相同。打乱后循环分配使使用量差至多一，
    # total <= 库存 * 上限 且 peak <= 库存即保证可行，无需逐轮贪心试错。
    for pool in pools.values():
        rng.shuffle(pool)
    offsets = Counter()
    scheduled = []
    for row in plan:
        batch = []
        for key in row:
            pool = pools[key]
            batch.append(deepcopy(pool[offsets[key] % len(pool)]))
            offsets[key] += 1
        scheduled.append(batch)
    return scheduled


def public_card(card: dict) -> dict:
    """只返回固定可见字段，拒绝元数据标识中携带答案或真假分组名。

    不透传 phase、stance、is_accurate 及任何未知字段；正文不进行改写。
    is_demo 若存在则保留，但不能替代调用方主程序的显式模拟标记。
    """
    if not isinstance(card, dict):
        raise ValueError("card 必须是字典")
    public = {}
    for key in _PUBLIC_FIELDS:
        value = card.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"公共字段 {key} 必须是非空字符串")
        if key != "text" and _LABEL_TOKEN.search(unquote(value)):
            raise ValueError(f"公共字段 {key} 不得包含答案标签或真假分组名")
        public[key] = deepcopy(value)
    if "is_demo" in card:
        if type(card["is_demo"]) is not bool:
            raise ValueError("is_demo 必须是 bool")
        public["is_demo"] = card["is_demo"]
    return public
