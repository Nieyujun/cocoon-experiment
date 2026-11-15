"""5 分钟答辩演示页 v3：高定科学期刊风 × 精密光学动态交互实验沙盘（showcase_5min.html）。

设计约束（步骤44，由 ``verify_five_minute_showcase`` 的 ``_ui_sandbox_audit`` 自动把关）：
- 四幕各自以确定性生成的定制 SVG 光学与流式沙盘为主画面，标题外的主屏说明 ≤40 字；
- 去粗黑铁框、去简笔画路标火柴人，采用衬线大标题排印、圆角悬浮卡片、双弧凸透镜与双环认知核徽章；
- 长文（模型原话、共享证据、三臂全文、答辩问答、诚实边界）全部收进默认关闭的侧滑抽屉；
- 提词器默认隐藏（T 键呼出）；零 Emoji、零彩色渐变字、零外链、零凭据；
- 数据 100% 由已核验的 dashboard_data.json、封闭情境清单与确定性材料重算，
  沙盘气泡只摘原话的逐字子串，不改写模型文本；
- 不修改任何受保护原件；生产文件逐字节可重现。
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import html
import json
import math
from pathlib import Path
import re

from . import closed_world_pilot as cwp, dashboard as db
from .runtime import canonical_hash

__all__ = [
    "SHOWCASE_CONTRACT",
    "build_showcase_payload",
    "render_five_minute_showcase",
    "verify_five_minute_showcase",
]

SHOWCASE_CONTRACT = "five_minute_showcase_v3"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD_DIR = ROOT / "experiment/dashboard_20261110"
DEFAULT_DATASET_PATH = DEFAULT_DASHBOARD_DIR / "dashboard_data.json"
DEFAULT_SHOWCASE_PATH = DEFAULT_DASHBOARD_DIR / "showcase_5min.html"
CLOSED_MANIFEST_PATH = ROOT / cwp.MANIFEST_RELATIVE
# 固定指向步骤42 归档的全量验证报告：新的 validate 运行不会让本页哈希漂移。
VALIDATION_PATH = ROOT / "experiment/review/validation_20261114T063037_ac1280.json"
PAIR_CHECKS = (
    "additional_length_matched", "assembly_valid", "dose_within_range",
    "evidence_equal", "length_matched", "response_equal", "source_equal",
)
ARMS = ("I0", "I5", "I4")
SUBJECT_IDS = ("S01", "S02")
LEDE_LIMIT = 45            # 审计硬上限（汉字）；实际主屏说明按 ≤40 字编写
BUBBLE_LIMIT = 24
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]")
GRADIENT_TEXT = re.compile(r"(?:-webkit-)?background-clip\s*:\s*text", re.I)
HARSH_BLOCK_SHADOW = re.compile(r"3px\s+3px\s+0\b")
STICK_FIGURE_PATH = re.compile(r"M14\s+-42\s+L72\s+-64")
CJK = re.compile(r"[\u4e00-\u9fff]")

# 被误导题 -> 澄清该题的核查证据编号（旁注逐字取自冻结证据原文）。
_ITEM_EVIDENCE = {"Q01": "XL_E01", "Q08": "XL_E01", "Q12": "XL_E01",
                  "Q03": "XL_E02", "Q04": "XL_E04", "Q06": "XL_E06"}
# 发帖中转述网传数字的原话（逐字子串）；前 3 条作为沙盘气泡。
_SPEECH_HIGHLIGHTS = {
    "S01": ["一期准点率常年九十八左右", "一期早高峰客流已过一万五千人次", "每年停运一个多月做检修",
            "智轨零排放", "每公里全周期三千多万元"],
    "S02": ["一期准点率百分之九十八", "客流一万五千", "每年停运一个多月检修"],
}
# 三轨气泡：受试回应原话的逐字子串（≤24 字）。
_TRACK_BUBBLES = {
    ("S01", "1"): {"I0": "我拿不准，想等更靠谱的核查结果再下结论",
                   "I5": "之前传的一万五千人次和九十八准点率都是误传",
                   "I4": "之前动摇主要是信了客流超一万五千的说法"},
    ("S01", "2"): {"I0": "我拿不准，想等更靠谱的核查结果再下结论",
                   "I5": "原来智轨零排放的说法不成立",
                   "I4": "我之前对乙方案的两点好感都被推翻了"},
    ("S02", "1"): {"I0": "没见官方核实，如果属实影响太大了",
                   "I5": "看完这几份材料我有点动摇了",
                   "I4": "那智轨运量更高的优势就没那么大了"},
    ("S02", "2"): {"I0": "没见官方核实，如果属实影响太大了",
                   "I5": "这点我之前确实被网传说法带偏了",
                   "I4": "我原先对乙方案的倾向明显动摇了"},
}
# 步骤42 已披露的待复核引用错位（S02 第 1 轮 I4 组织段）。
_REVIEW_FLAG = ("S02", "1", "网传每年停运一个多月，实为把一次性新车调试期误算成年度例行维护。（XL_E03）")
_ARM_NAMES = {"I0": "I0 同温层回放", "I5": "I5 官方原句摘录", "I4": "I4 破茧助手"}
_ROUND_THEMES = {"1": "客流 · 成本 · 工期", "2": "碳排 · 覆盖 · 停运"}


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError("showcase:" + code)


def _r(value, digits=1):
    return round(float(value), digits)


# ============================================================ 数据层
def _load_verified_dataset(dataset_path: Path) -> dict:
    path = Path(dataset_path)
    _need(path.is_file(), "dataset_missing")
    dataset = json.loads(path.read_text(encoding="utf-8"))
    expected = canonical_hash({k: v for k, v in dataset.items() if k != "dataset_sha256"})
    _need(dataset.get("dataset_sha256") == expected, "dataset_hash_mismatch")
    _need(dataset.get("closed_world_pilot", {}).get("available") is True, "closed_world_unavailable")
    _need(dataset.get("live_micro_matrix", {}).get("available") is True, "live_matrix_unavailable")
    _need(dataset.get("dual_track", {}).get("available") is True, "dual_track_unavailable")
    return dataset


def _evidence_list() -> list[dict]:
    return [{"evidence_id": e[0], "round": e[1], "publisher": e[2], "date": e[3], "text": e[7]}
            for e in cwp.EVIDENCE]


def _subject_feed(materials: dict, subject_id: str) -> list[dict]:
    meta = {card["card_id"]: card for card in materials["cards"]}
    stance0 = cwp.STANCES[subject_id]
    out = []
    for round_idx, cards in enumerate(materials["schedules"][subject_id]["exposure"], start=1):
        for slot_idx, public in enumerate(cards, start=1):
            card = meta[public["card_id"]]
            relation = "same" if card["stance"] == stance0 else ("neutral" if card["stance"] == 0 else "opposite")
            evidence_ids = []
            for item in card.get("targeted_items") or ():
                _need(item in _ITEM_EVIDENCE, "misled_item_without_evidence_" + item)
                if _ITEM_EVIDENCE[item] not in evidence_ids:
                    evidence_ids.append(_ITEM_EVIDENCE[item])
            _need(card["is_accurate"] == (not evidence_ids), "misleading_flag_mismatch_" + card["card_id"])
            out.append({"round": round_idx, "slot": slot_idx, "card_id": card["card_id"], "text": public["text"],
                        "misleading": not card["is_accurate"], "relation": relation,
                        "targeted_items": list(card.get("targeted_items") or ()), "evidence_ids": evidence_ids})
    return out


def _mirror_label(text: str) -> str:
    label = re.split(r"[：（(]", text, maxsplit=1)[0][:9]
    _need(bool(label) and text.startswith(label), "mirror_label_not_prefix")
    return label


def _mirror_payload(items: list[dict]) -> dict:
    stance = [x for x in items if x["section"] == "stance_analysis"]
    pct = None
    if stance:
        m = re.search(r"(\d{1,3})\s*[%％]?处|约\s*(\d{1,3})|(\d{1,3})\s*%", stance[0]["text"])
        if m:
            pct = int(next(g for g in m.groups() if g))
    _need(pct is not None and 0 <= pct <= 100, "mirror_stance_pct_missing")
    biases = [x for x in items if x["section"] == "possible_biases"][:3]
    blinds = [x for x in items if x["section"] == "blindspot_alert"][:2]
    return {
        "stance_pct": pct,
        "targets": ([{"kind": "bias", "label": _mirror_label(b["text"]), "index": items.index(b)} for b in biases]
                    + [{"kind": "blindspot", "label": f"盲区 {k}", "index": items.index(b)}
                       for k, b in enumerate(blinds, 1)]),
        "items": [{"section": x["section"], "text": x["text"], "user_quote": x.get("user_quote"),
                   "citations": list(x.get("citations") or [])} for x in items],
    }


def _t3_by_arm(closed: dict, subject_id: str) -> dict:
    comp = {(r["subject"], r["arm"], r["timepoint"]): r for r in closed["comparison"]}
    items = {(r["subject"], r["arm"], r["timepoint"]): r for r in closed["item_level"]}
    out = {}
    for arm in ARMS:
        row, item = comp[(subject_id, arm, "T3")], items[(subject_id, arm, "T3")]
        out[arm] = {"Acc": _r(row["Acc"]), "BS": _r(row["BS"], 4), "U": _r(row["U"]), "O": _r(row["O"]),
                    "transfer_correct": int(item["transfer_stats"]["correct"]),
                    "transfer_total": int(item["transfer_stats"]["n"])}
    return out


def _build_subjects_payload(closed: dict, materials: dict, manifest: dict) -> list[dict]:
    subjects = []
    for subj in closed["subjects"]:
        sid = subj["subject"]
        _need(sid in SUBJECT_IDS, "unexpected_subject")
        audit = closed["exposure_audit"][sid]["exposure"]
        feed = _subject_feed(materials, sid)
        _need(len(feed) == audit["card_deliveries"] == 48, "feed_count_mismatch")
        same = sum(c["relation"] == "same" for c in feed)
        rumor = sum(c["misleading"] for c in feed)
        _need(same == audit["same_stance_deliveries"] and rumor == audit["misleading_card_deliveries"],
              "exposure_audit_mismatch")
        interventions = manifest["interventions"][sid]
        rounds = {}
        for r_key in ("1", "2"):
            r = int(r_key)
            checks = subj["pair_checks"][r_key]
            _need(all(checks.get(key) is True for key in PAIR_CHECKS), "pair_checks_failed")
            for arm in ("I4", "I5"):
                record = interventions[arm][r_key]
                text = record["visible_text"]
                _need(len(text) == record["char_count"], "visible_chars_mismatch")
                _need(hashlib.sha256(text.encode("utf-8")).hexdigest() == record["visible_text_sha256"],
                      "visible_text_hash_mismatch")
                _need(materials["blocks"][r] in text, "shared_evidence_mismatch")
                addition = text.split("\n\n阅读说明\n", 1)[1].rsplit("\n\n", 1)[0]
                _need(addition == subj[arm.lower() + "_additions"][r_key], "addition_mismatch")
            i0 = materials["i0"][sid][r]
            responses = {arm: subj["intervention_responses"][arm][r - 1] for arm in ARMS}
            bubbles = _TRACK_BUBBLES[(sid, r_key)]
            for arm in ARMS:
                _need(len(bubbles[arm]) <= BUBBLE_LIMIT and bubbles[arm] in responses[arm], "track_bubble_not_verbatim")
            i4_add = subj["i4_additions"][r_key]
            flag = None
            if (sid, r_key) == _REVIEW_FLAG[:2]:
                _need(_REVIEW_FLAG[2] in i4_add, "review_flag_sentence_missing")
                flag = {"sentence": _REVIEW_FLAG[2], "cited": "XL_E03", "fact_source": "XL_E06",
                        "note": "该句的停运事实出自第 2 轮 XL_E06，却标为本轮 XL_E03（XL_E03 实为工期）；"
                                "自动配对检查通过不代表每句引用正确，待真人复核。"}
            rounds[r_key] = {
                "round": r, "theme": _ROUND_THEMES[r_key],
                "shared_evidence_block": materials["blocks"][r],
                "shared_evidence_ids": [e[0] for e in cwp.EVIDENCE if e[1] == r],
                "i0_visible_text": i0["visible_text"], "i0_chars": i0["char_count"],
                "i5_addition": subj["i5_additions"][r_key], "i5_chars": interventions["I5"][r_key]["char_count"],
                "i4_addition": i4_add, "i4_chars": interventions["I4"][r_key]["char_count"],
                "i4_questions": i4_add.count("？") + i4_add.count("?"),
                "responses": responses, "bubbles": dict(bubbles),
                "pair_checks": {k: checks[k] for k in PAIR_CHECKS},
                "total_difference_ratio": checks["total_difference_ratio"],
                "additional_difference_ratio": checks["additional_difference_ratio"],
                "all_seven_checks_passed": True, "review_flag": flag,
            }
        branches = [b for b in manifest["branches"] if b["subject"] == sid]
        _need({b["arm"] for b in branches} == set(ARMS), "branch_set_mismatch")
        parents = {b["pre_treatment_core_sha256"] for b in branches}
        _need(len(parents) == 1 and len(next(iter(parents))) == 64, "parent_snapshot_mismatch")
        _need(next(iter(parents)) == manifest["parent_t1_core_sha256"][sid], "parent_snapshot_mismatch")
        t1 = next(r for r in closed["item_level"] if r["subject"] == sid and r["timepoint"] == "T1")
        highlights = list(_SPEECH_HIGHLIGHTS[sid])
        _need(all(h in subj["speech"] for h in highlights), "speech_highlight_not_verbatim")
        is_pos = subj["initial_side"] == "positive"
        subjects.append({
            "subject_id": sid, "initial_side": subj["initial_side"],
            "label": f"{sid} · 初挺{'甲' if is_pos else '乙'}方案",
            "side_text": "甲方案·双轨快速公交" if is_pos else "乙方案·氢能智轨",
            "parent_snapshot_sha256": next(iter(parents)),
            "exposure": {"total": len(feed), "same": same, "rumor": rumor,
                         "overlap": sum(c["relation"] == "same" and c["misleading"] for c in feed),
                         "neutral": sum(c["relation"] == "neutral" for c in feed),
                         "opposite": sum(c["relation"] == "opposite" for c in feed),
                         "retention_rumor": closed["exposure_audit"][sid]["retention"]["misleading_card_deliveries"]},
            "feed": feed, "speech": subj["speech"], "speech_highlights": highlights,
            "bubbles": highlights[:3],
            "t1_items": len(t1["p_true"]),
            "t1_items_at_half": sum(1 for v in t1["p_true"].values() if v == 0.5),
            "mirror": _mirror_payload(subj["mirror_items"]),
            "rounds": rounds,
            "i0_responses_identical": rounds["1"]["responses"]["I0"] == rounds["2"]["responses"]["I0"],
            "t3": _t3_by_arm(closed, sid),
        })
    _need([s["subject_id"] for s in subjects] == list(SUBJECT_IDS), "subject_order_mismatch")
    return subjects


def _build_scoreboard(closed: dict) -> list[dict]:
    rows = {row["arm"]: row for row in closed["descriptive_by_arm"]}
    out = []
    for arm in ARMS:
        row = rows[arm]
        t3 = [r for r in closed["item_level"] if r["arm"] == arm and r["timepoint"] == "T3"]
        _need(len(t3) == 2, "missing_item_level_" + arm)
        entry = {"arm": arm, "name": _ARM_NAMES[arm],
                 "transfer_correct_t3": _r(sum(r["transfer_stats"]["correct"] for r in t3) / len(t3), 2),
                 "transfer_total": t3[0]["transfer_stats"]["n"],
                 "u_t3": _r(row["U_T3_mean"]), "o_t3": _r(row["O_T3_mean"]),
                 "misled_t1": _r(row["misled_correct_T1_mean"]), "misled_t3": _r(row["misled_correct_T3_mean"])}
        for tp in ("T0", "T1", "T2", "T3"):
            entry["acc_" + tp.lower()] = _r(row[f"Acc_{tp}_mean"])
            entry["bs_" + tp.lower()] = _r(row[f"BS_{tp}_mean"], 4)
            entry["s_" + tp.lower()] = _r(row[f"S_{tp}_mean"])
        out.append(entry)
    return out


def _particles(same_only: int, both: int, rumor_only: int, neither: int, slots: int = 24) -> list[str]:
    counts = {"same": same_only, "both": both, "rumor": rumor_only, "neither": neither}
    total = sum(counts.values())
    raw = {k: v * slots / total for k, v in counts.items()}
    alloc = {k: int(math.floor(v)) for k, v in raw.items()}
    for k in sorted(raw, key=lambda key: (-(raw[key] - alloc[key]), key))[: slots - sum(alloc.values())]:
        alloc[k] += 1
    flat = [k for k in ("same", "both", "rumor", "neither") for _ in range(alloc[k])]
    return [flat[(i * 7) % slots] for i in range(slots)]


def _scenes() -> list[dict]:
    return [
        {"index": 0, "code": "01", "name": "克隆分流", "title": "同一个受试，克隆成三条平行轨道",
         "lede": "同一受试状态克隆三条轨道，同源等量证据公平对决。", "window": "0:00–1:00",
         "solo": ("各位老师好，我们展示的是“破茧助手”实验沙盘。左边是同质信息培养皿：受试连续刷到同阵营帖和含失真数字的帖子，"
                  "两类帖子可以重叠。刷完 8 轮后，我们把受试此刻的完整状态存成父快照，再克隆成三条轨道：I0 继续刷同温层，"
                  "I5 只读官方原句摘录，I4 由破茧助手按检索、贴标、扫盲区、整合、认知镜五步介入。右下天平说明公平性："
                  "I4 和 I5 读同一个证据块，可见字数都在 600 到 800 字之间，最大差 2.6%。"),
         "team": ("【A 实验控制 · 60 秒】我们想回答：一个人只刷同类观点和网传数字时，AI 助手能不能帮他纠偏。"
                  "沙盘左边是培养皿，中间是父快照克隆门，我点一下“演示克隆分流”，同一个受试分成三条轨道。"
                  "I4 和 I5 共享逐字相同的证据块，长度差不超过 2.6%，比较的只是“有没有针对性的组织与追问”。")},
        {"index": 1, "code": "02", "name": "茧房现场", "title": "48 张帖子之后：发帖松动，答卷审慎",
         "lede": "发帖转述了网传数字，正式答卷却仍停在五成。", "window": "1:00–2:15",
         "solo": ("第 2 幕是星澜市现场。这是虚构的封闭城市，模型没有任何先验。点播放，8 轮共 48 张帖子逐列推进，"
                  "珊瑚红脉冲是 16 张含失真数字的帖子。刷完以后，受试发帖时转述了网传的准点率和客流数字，上方三个气泡都是原话；"
                  "但正式答卷时 12 道题全部填 0.50，指针停在正中。发帖被带偏，答卷保持审慎，预期的茧房致盲在测量层没有出现。"
                  "右边是认知镜光学诊断，点任一条目可以看原文与引句。"),
         "team": ("【B 数据与真值 · 75 秒】星澜市的数据全部是合成的，模型无从猜测。我点播放，失真帖子的脉冲汇入受试。"
                  "上方气泡是他发帖的原话，下方仪表盘是答卷：12 题全是 0.50。我们没有在看到结果后改规则去追求致盲。"
                  "右侧认知镜给出立场刻度和偏差诊断，老师想看原文，我点开抽屉。")},
        {"index": 2, "code": "03", "name": "平行对决", "title": "同一起点，三种平行干预对决",
         "lede": "起点哈希相同；只有拿到核查证据的两条轨道纠正了事实。", "window": "2:15–3:45",
         "solo": ("三条轨道起点哈希完全一致。点播放干预脉冲：上轨 I0 继续读同温层，受试还是拿不准；中轨 I5 读到共享证据块的官方原句；"
                  "下轨 I4 读同一个证据块，再经过光学认知镜，点名他原话里的口径误区并追问一句。末端是第三次测量：I5 和 I4 核心事实都到 100%，"
                  "迁移题 I4 答对 4 道、I5 答对 3 道。气泡是受试回应的原话片段，完整原文在抽屉里；S02 第 1 轮有一处证据编号错位，待真人复核。"),
         "team": ("【C 助手适配 · 90 秒】左边晶核是 T1 父快照哈希，三条泳道从这里分叉。I4 和 I5 之间的跨轨桥接舱代表同一个证据块。"
                  "I4 多出来的只有认知镜透镜这一步：点名误区、提出反思问题。我切到 S02 第 2 轮再播一次。"
                  "完整三轨原文在这个按钮里，包括我们自己发现的一处引用编号错位。")},
        {"index": 3, "code": "04", "name": "双轨终局", "title": "拆掉先验天花板：双轨曲线与终局账本",
         "lede": "现实议题撞上先验天花板；封闭新题里证据组全对，主终点打平。", "window": "3:45–5:00",
         "solo": ("左图是三个现实议题：模型实验前就答对 100%，碰到了预训练先验天花板。右图是星澜市：T0 和 T1 都在 50% 盲猜线，"
                  "拿到证据后 I5、I4 升到 100%，珊瑚色区域是 4 轮误导帖回流，没有回退。下面四项终局指标：主终点 Brier 两组都是 0.0025，打平；"
                  "迁移题和换位理解 I4 较高；盲区遗漏率反而是 I5 更低。受试是 mimo-v2.6-flash 模型，每议题两人，不做统计推断。"
                  "账本 383 次结算、1.58 元，可以随时切回审计看板核对。"),
         "team": ("【E 评分与统计 + D 记录与可视化 · 75 秒】E：主终点打平，探索性指标互有高低，遗漏率 I5 更好，我们不挑有利的报。"
                  "D：所有数字都能在账本和哈希链里追到，点“审计看板”可以逐条核对。谢谢各位老师。")},
    ]


def _qa_cards() -> list[dict]:
    return [
        {"q": "Q1 主终点上 I4 为什么和 I5 打平？",
         "a": ("两组读到的是逐字相同的官方核查证据。对这个模型受试来说，只要看到核查数字就能把 8 道锚题答对（50%→100%）。"
               "预设主终点是 T3 的 Brier 分数，两组都是 0.0025（干预前 0.2500）。探索性指标里，迁移题 I4 为 4/4、I5 为 3/4，"
               "换位理解 U 为 100 对 91.7，但盲区遗漏率 O 是 I5 更低（33.3 对 58.3）。每组只有 2 名模型受试，"
               "不能据此确认组织与反思段的额外作用。")},
        {"q": "Q2 刷了 8 轮误导帖，为什么答卷没有被带偏？",
         "a": ("受试发帖时确实转述了网传数字；但封闭情境规则要求“未见核查证据的题填 0.45–0.55”，测量时它以“没有官方核查”为由保持 0.50。"
               "所以 T1 没有出现预期的茧房致盲。我们判断这条规则压住了误导效应，但没有在看到结果后修改规则重跑，那属于事后调参。"
               "如需检验致盲，应先预登记新规则，作为封闭情境 v2 单独运行。")},
        {"q": "Q3 怎么保证没有故意把 I5 做差？",
         "a": ("I5 不是人工写的：由确定性算法从同一份冻结证据包中整句摘录，与 I4 共享同一证据块和同一来源，"
               "每轮可见字数落在 600–800 字，总长差最大 2.6%（门禁 ≤10%），四轮全部通过七项自动配对检查。")},
        {"q": "Q4 这项工作的边界是什么？",
         "a": ("受试由 mimo-v2.6-flash 扮演，真人受试与真人复核均为 0（formal_eligible=False）；每议题 n=2，只做机制演示，"
               "不做统计推断；星澜市是方案 §5.4 的封闭合成情境，单独报告，不作为现实政策证据。")},
        {"q": "Q5 原文里有没有待复核的问题？",
         "a": ("有。S02 第 1 轮 I4 组织段把年度停运的说明标为 XL_E03，但 XL_E03 实为工期，停运事实出自第 2 轮的 XL_E06。"
               "自动配对检查通过不等于每句引用都正确。另外 I5 两名受试在迁移题 Q09 上理由与概率自相矛盾，I0 两轮回应逐字相同。"
               "页面保留原文，等待真人复核。")},
    ]


def build_showcase_payload(dataset_path: Path = DEFAULT_DATASET_PATH) -> dict:
    """从已核验的 dashboard_data.json、封闭情境清单与确定性材料构建演示数据包。"""
    dataset = _load_verified_dataset(dataset_path)
    materials = cwp.build_closed_world_materials()
    closed, matrix, dual = dataset["closed_world_pilot"], dataset["live_micro_matrix"], dataset["dual_track"]
    overview = dataset["overview"]
    billing = overview["billing"]
    _need(CLOSED_MANIFEST_PATH.is_file(), "closed_manifest_missing")
    manifest = json.loads(CLOSED_MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_hash = canonical_hash({k: v for k, v in manifest.items() if k != "manifest_sha256"})
    _need(manifest_hash == manifest.get("manifest_sha256") == closed["manifest_sha256"], "closed_manifest_hash_mismatch")
    _need(materials["materials_sha256"] == closed["materials_sha256"], "materials_hash_mismatch")
    gates = manifest["honest_gates"]
    _need(gates["formal_eligible"] is False and gates["is_synthetic_closed_world"] is True
          and gates["human_reviews_completed"] == 0, "honest_gates_changed")
    validation = json.loads(VALIDATION_PATH.read_text(encoding="utf-8"))
    _need(validation.get("success") is True, "historical_validation_failed")

    subjects = _build_subjects_payload(closed, materials, manifest)
    scoreboard = _build_scoreboard(closed)
    real_by_arm = {row["arm"]: row for row in dual["real"]["by_arm"]}
    synth_by_arm = {row["arm"]: row for row in closed["descriptive_by_arm"]}
    tps = ("T0", "T1", "T2", "T3")
    real_calls_final = int(matrix["totals"]["final_run_model_calls"])
    closed_calls = int(closed["actual_model_calls"])
    closed_answers = sum(n["kind"] in ("subject_measurement", "blind_evaluation") for n in closed["trace"])
    pairs = [{"subject": s["subject_id"], "round": rc["round"], "i4_chars": rc["i4_chars"], "i5_chars": rc["i5_chars"],
              "total_difference_ratio": rc["total_difference_ratio"]} for s in subjects for rc in s["rounds"].values()]
    chars = [p[k] for p in pairs for k in ("i4_chars", "i5_chars")]
    venn = {k: sum(s["exposure"][k] for s in subjects) for k in ("total", "same", "rumor", "overlap")}
    neither = venn["total"] - venn["same"] - venn["rumor"] + venn["overlap"]
    sb = {row["arm"]: row for row in scoreboard}

    core = {
        "contract": SHOWCASE_CONTRACT,
        "source_dataset_sha256": dataset["dataset_sha256"],
        "closed_manifest_sha256": closed["manifest_sha256"],
        "matrix_sha256": matrix.get("matrix_sha256", ""),
        "closed_run_id": closed["run_id"],
        "honest_badge": ("mimo-v2.6-flash 扮演受试 · 每议题 n=2 · §5.4 封闭合成情境 · "
                         "真人受试/复核 0 · formal_eligible=False"),
        "scenes": _scenes(),
        "scale": {"topics": 4, "final_run_calls": real_calls_final + closed_calls,
                  "real_final_run_calls": real_calls_final, "real_accepted_calls": int(matrix["totals"]["accepted_calls"]),
                  "real_branches": int(matrix["totals"]["branches"]), "closed_calls": closed_calls,
                  "answers_and_blind_evaluations": (int(matrix["totals"]["subject_measurements"])
                                                    + int(matrix["totals"]["blind_evaluations"]) + closed_answers),
                  "offline_rehearsal_samples": int(overview["panel_samples"]),
                  "archived_tests": int(validation["tests_run"]),
                  "archived_validation": VALIDATION_PATH.relative_to(ROOT).as_posix(),
                  "real_topics": [{"topic_id": t["topic_id"], "label": t["topic_label"],
                                   "calls": t["actual_model_calls"], "subjects": " / ".join(t["subject_ids"])}
                                  for t in matrix["topic_summaries"]]},
        "clone": {"particles": _particles(venn["same"] - venn["overlap"], venn["overlap"],
                                          venn["rumor"] - venn["overlap"], neither),
                  "venn": dict(venn, neither=neither),
                  "pairs": pairs, "char_min": min(chars), "char_max": max(chars),
                  "max_difference_pct": _r(100 * max(p["total_difference_ratio"] for p in pairs)),
                  "i4_mean_chars": _r(sum(p["i4_chars"] for p in pairs) / len(pairs)),
                  "i5_mean_chars": _r(sum(p["i5_chars"] for p in pairs) / len(pairs)),
                  "pair_rounds_passed": len(pairs), "dose_window": [600, 800], "tolerance_pct": 10},
        "evidence": _evidence_list(),
        "subjects": subjects,
        "real_track": {"label": "现实三议题", "accepted_calls": int(matrix["totals"]["accepted_calls"]),
                       "branches": int(matrix["totals"]["branches"]),
                       "acc_series": {a: [_r(real_by_arm[a][f"Acc_{t}_mean"]) for t in tps] for a in ARMS},
                       "bs_series": {a: [_r(real_by_arm[a][f"BS_{t}_mean"], 4) for t in tps] for a in ARMS},
                       "by_topic": [{"topic_id": r["topic_id"], "arm": r["arm"], "acc_t0": _r(r["Acc_T0_mean"]),
                                     "acc_t3": _r(r["Acc_T3_mean"]), "bs_t0": _r(r["BS_T0_mean"], 4),
                                     "bs_t3": _r(r["BS_T3_mean"], 4)} for r in matrix["descriptive_by_topic_arm"]]},
        "synth_track": {"label": "星澜市封闭新题", "calls": closed_calls,
                        "acc_series": {a: [_r(synth_by_arm[a][f"Acc_{t}_mean"]) for t in tps] for a in ARMS},
                        "bs_series": {a: [_r(synth_by_arm[a][f"BS_{t}_mean"], 4) for t in tps] for a in ARMS},
                        "reflux_rounds": 4},
        "scoreboard": scoreboard,
        "final_rows": [
            {"key": "acc", "label": "核心事实准确率", "max": 100, "unit": "%",
             "values": {a: sb[a]["acc_t3"] for a in ARMS}, "lower_better": False,
             "note": (f"主终点 Brier 同为 {sb['I4']['bs_t3']:.4f} · 打平" if sb["I4"]["bs_t3"] == sb["I5"]["bs_t3"]
                      else f"主终点 Brier I4 {sb['I4']['bs_t3']:.4f} / I5 {sb['I5']['bs_t3']:.4f}")},
            {"key": "transfer", "label": "迁移新题答对", "max": sb["I4"]["transfer_total"], "unit": "/4",
             "values": {a: sb[a]["transfer_correct_t3"] for a in ARMS}, "lower_better": False,
             "note": (f"I4 {sb['I4']['transfer_correct_t3']:g}/4 · I5 {sb['I5']['transfer_correct_t3']:g}/4 · 探索性")},
            {"key": "u", "label": "换位理解 U", "max": 100, "unit": "",
             "values": {a: sb[a]["u_t3"] for a in ARMS}, "lower_better": False,
             "note": f"I4 {sb['I4']['u_t3']:g} · I5 {sb['I5']['u_t3']:g} · 探索性"},
            {"key": "o", "label": "盲区遗漏率 O", "max": 100, "unit": "",
             "values": {a: sb[a]["o_t3"] for a in ARMS}, "lower_better": True,
             "note": (f"越低越好：I5 {sb['I5']['o_t3']:g} 优于 I4 {sb['I4']['o_t3']:g}"
                      if sb["I5"]["o_t3"] < sb["I4"]["o_t3"]
                      else f"越低越好：I4 {sb['I4']['o_t3']:g} / I5 {sb['I5']['o_t3']:g}")},
        ],
        "qa_cards": _qa_cards(),
        "boundary": [
            "受试由 mimo-v2.6-flash 大模型扮演；真人受试 0、真人复核 0；formal_eligible=False，不作为正式实验结论。",
            "每个议题 n=2、三臂，只做机制演示与工程闭环验证，不做统计显著性推断。",
            "星澜市（TOPIC_SYNTH_01）是方案 §5.4 的封闭合成情境，全部材料标 synthetic，来源为 .invalid 保留域名；"
            "结果单独报告，不作为现实政策证据。§5.4 表中“模型既有知识过强”一行原写实体替换的半合成情境，本次采用表下封闭示例。",
            "T1 未出现预期的茧房致盲：测量层 12 题全部为 0.50；未在看到结果后修改封闭规则重跑。",
            f"主终点 BS_T3：I4 与 I5 同为 {sb['I4']['bs_t3']:.4f}，并列；探索性指标互有高低："
            f"迁移 {sb['I4']['transfer_correct_t3']:g}/4 对 {sb['I5']['transfer_correct_t3']:g}/4，"
            f"U {sb['I4']['u_t3']:g} 对 {sb['I5']['u_t3']:g}，盲区遗漏率 O {sb['I4']['o_t3']:g} 对 {sb['I5']['o_t3']:g}（I5 更低）。",
            "I5 两名受试在迁移题 Q09 上理由与概率自相矛盾，I4 的迁移题优势需打折看待。",
            "S02 第 1 轮 I4 组织段把 XL_E06 的停运事实标为 XL_E03，待真人复核；I0 两轮回应逐字相同。",
            "步骤42 封闭情境共 5 次运行，4 次失败或中断全部留痕且费用并入；最终运行复用首跑已通过冻结校验的四阶段输出。",
            "本页为离线生成的演示视图，生成与核验费用 ¥0.00；全部数字可在审计看板 dashboard.html 中逐条追溯。",
        ],
        "audit": {"dashboard_href": "./dashboard.html", "settled_calls": billing["settled_calls"],
                  "total_charged_cny": billing["total_charged_cny"],
                  "budget_limit_cny": billing["authorized_limit_cny"],
                  "held_unsettled_reserved_cny": billing["held_unsettled_reserved_cny"],
                  "ledger_sha256": billing["ledger_sha256"]},
    }
    _need(all(s["i0_responses_identical"] for s in subjects), "i0_identical_disclosure_stale")
    payload = deepcopy(core)
    payload["showcase_sha256"] = canonical_hash(core)
    return payload


# ============================================================ 精密光学与几何 SVG 组件库
def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _n(value) -> str:
    if isinstance(value, float):
        text = ("%.2f" % value).rstrip("0").rstrip(".")
        return text if text not in ("-0", "") else "0"
    return str(value)


def _t(x, y, text, cls="lbl", anchor="start", extra="") -> str:
    return f'<text x="{_n(x)}" y="{_n(y)}" class="{cls}" text-anchor="{anchor}"{extra}>{_e(text)}</text>'


def _fmt(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _visual_units(text: str) -> float:
    return sum(1.0 if ord(ch) > 127 else 0.56 for ch in text)


def _svg_defs(prefix: str) -> str:
    """每幕独立的光学渐变与弥散微阴影定义，避免跨幕隐藏 SVG 的 ID 冲突。"""
    return (
        "<defs>"
        f'<filter id="{prefix}-shadow" x="-12%" y="-12%" width="124%" height="128%">'
        '<feDropShadow dx="0" dy="4" stdDeviation="7" flood-color="#0F172A" flood-opacity="0.06"/>'
        '<feDropShadow dx="0" dy="1" stdDeviation="1.5" flood-color="#0F172A" flood-opacity="0.04"/>'
        "</filter>"
        f'<filter id="{prefix}-pill-sh" x="-15%" y="-20%" width="130%" height="150%">'
        '<feDropShadow dx="0" dy="2" stdDeviation="3.5" flood-color="#0F172A" flood-opacity="0.07"/>'
        "</filter>"
        f'<linearGradient id="{prefix}-lens" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0%" stop-color="#147D84" stop-opacity="0.22"/>'
        '<stop offset="52%" stop-color="#FFFFFF" stop-opacity="0.65"/>'
        '<stop offset="100%" stop-color="#C87D16" stop-opacity="0.26"/>'
        "</linearGradient>"
        f'<linearGradient id="{prefix}-spec" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0%" stop-color="#147D84" stop-opacity="0.88"/>'
        '<stop offset="50%" stop-color="#94A3B8" stop-opacity="0.45"/>'
        '<stop offset="100%" stop-color="#C87D16" stop-opacity="0.92"/>'
        "</linearGradient>"
        f'<radialGradient id="{prefix}-halo-amber" cx="50%" cy="50%" r="50%">'
        '<stop offset="0%" stop-color="#C87D16" stop-opacity="0.24"/>'
        '<stop offset="100%" stop-color="#C87D16" stop-opacity="0"/>'
        "</radialGradient>"
        f'<radialGradient id="{prefix}-halo-teal" cx="50%" cy="50%" r="50%">'
        '<stop offset="0%" stop-color="#147D84" stop-opacity="0.22"/>'
        '<stop offset="100%" stop-color="#147D84" stop-opacity="0"/>'
        "</radialGradient>"
        f'<radialGradient id="{prefix}-halo-red" cx="50%" cy="50%" r="50%">'
        '<stop offset="0%" stop-color="#D6453A" stop-opacity="0.22"/>'
        '<stop offset="100%" stop-color="#D6453A" stop-opacity="0"/>'
        "</radialGradient>"
        f'<linearGradient id="{prefix}-area-teal" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#147D84" stop-opacity="0.16"/>'
        '<stop offset="100%" stop-color="#147D84" stop-opacity="0.0"/>'
        "</linearGradient>"
        f'<linearGradient id="{prefix}-area-amber" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#C87D16" stop-opacity="0.15"/>'
        '<stop offset="100%" stop-color="#C87D16" stop-opacity="0.0"/>'
        "</linearGradient>"
        "</defs>"
    )


def _agent_node(cx: float, cy: float, scale: float = 1.0, tone: str = "neutral",
                prefix: str = "s0", badge: str = "") -> str:
    """双环光学认知核徽章（取代旧版梯形火柴人）：呼吸光晕 + 方位刻度外环 + 圆润半身人物剪影章。"""
    halo = ""
    if tone in ("amber", "teal", "red"):
        halo = f'<circle cx="0" cy="0" r="38" fill="url(#{prefix}-halo-{tone})"/>'
    clip_id = f"{prefix}-av-{int(round(cx))}-{int(round(cy))}"
    badge_el = ""
    if badge == "?":
        badge_el = ('<circle class="av-tag-bg av-tag-muted" cx="18" cy="-18" r="9"/>'
                    '<text x="18" y="-14" class="av-tag-t" text-anchor="middle">?</text>')
    elif badge == "ok":
        cls = "av-tag-amber" if tone == "amber" else "av-tag-teal"
        badge_el = (f'<circle class="av-tag-bg {cls}" cx="18" cy="-18" r="8.5"/>'
                    '<path class="av-check" d="M14.2 -18 L17 -15.2 L22 -20.5"/>')
    return (
        f'<g class="agent ag-{tone}" transform="translate({_n(cx)} {_n(cy)}) scale({_n(scale)})">'
        f"{halo}"
        '<circle class="ag-orbit" cx="0" cy="0" r="30"/>'
        f'<circle class="ag-disc" cx="0" cy="0" r="22" filter="url(#{prefix}-pill-sh)"/>'
        f'<clipPath id="{clip_id}"><circle cx="0" cy="0" r="19"/></clipPath>'
        f'<g clip-path="url(#{clip_id})">'
        '<circle class="ag-head" cx="0" cy="-4.5" r="6.2"/>'
        '<path class="ag-bust" d="M-13.5 18 C-12 7.5 -6.5 4.2 0 4.2 C6.5 4.2 12 7.5 13.5 18 Z"/>'
        "</g>"
        '<circle class="ag-rim" cx="0" cy="0" r="22"/>'
        f"{badge_el}"
        "</g>"
    )


def _optical_lens(cx: float, cy: float, h: float = 84.0, prefix: str = "s1", label: str = "认知镜") -> str:
    """双弧凸透镜组（取代旧版死板的等边三角形）：上下精密镜座 + 晶莹玻璃折射面 + 高光弧。"""
    hh = h / 2.0
    bw = round(h * 0.28, 1)
    hl = round(hh * 0.64, 1)
    lbl = _t(cx, cy + hh + 20, label, "lbl-xs", "middle") if label else ""
    return (
        f'<g class="lens-unit">'
        f'<ellipse cx="{_n(cx)}" cy="{_n(cy)}" rx="{_n(bw + 10)}" ry="{_n(hh + 6)}" fill="url(#{prefix}-halo-amber)"/>'
        f'<path class="lens-body" fill="url(#{prefix}-lens)" '
        f'd="M{_n(cx)} {_n(cy - hh)} Q{_n(cx + bw)} {_n(cy)} {_n(cx)} {_n(cy + hh)} '
        f'Q{_n(cx - bw)} {_n(cy)} {_n(cx)} {_n(cy - hh)} Z"/>'
        f'<path class="lens-spec" d="M{_n(cx - bw * 0.28)} {_n(cy - hl)} '
        f'Q{_n(cx - bw * 0.58)} {_n(cy)} {_n(cx - bw * 0.28)} {_n(cy + hl)}"/>'
        f'<line class="lens-axis" x1="{_n(cx)}" y1="{_n(cy - hh + 4)}" x2="{_n(cx)}" y2="{_n(cy + hh - 4)}"/>'
        f'<rect class="lens-cap" x="{_n(cx - 8)}" y="{_n(cy - hh - 4)}" width="16" height="5" rx="2.5"/>'
        f'<rect class="lens-cap" x="{_n(cx - 8)}" y="{_n(cy + hh - 1)}" width="16" height="5" rx="2.5"/>'
        f"{lbl}</g>"
    )


def _callout_pill(cx: float, y: float, text: str, prefix: str, tone: str = "red", font: float = 13.5) -> str:
    """居中悬浮圆角原话气泡卡片（精确测算中英文视觉字宽）。"""
    quoted = "「" + text + "」"
    w = round(_visual_units(quoted) * font + 28, 1)
    x0 = round(cx - w / 2.0, 1)
    return (
        f'<g class="bubble b-tone-{tone}">'
        f'<rect class="bubble-card" x="{_n(x0)}" y="{_n(y)}" width="{_n(w)}" height="34" rx="10" '
        f'filter="url(#{prefix}-pill-sh)"/>'
        f'<rect class="bubble-bar" x="{_n(x0 + 6)}" y="{_n(y + 8)}" width="3" height="18" rx="1.5"/>'
        + _t(cx + 2, y + 22, quoted, "bubble-t", "middle")
        + "</g>"
    )


def _track_quote_card(x: float, cy: float, max_w: float, text: str, prefix: str, arm: str) -> str:
    """泳道内左指向回应原话气泡卡（位于受试右侧，因果链从左至右顺畅阅读）。"""
    quoted = "「" + text + "」"
    font = 12.6
    w = min(max_w, max(190.0, round(_visual_units(quoted) * font + 26, 1)))
    y0 = round(cy - 20, 1)
    return (
        f'<g class="bubble quote q-{arm}">'
        f'<path class="q-tail" d="M{_n(x - 7)} {_n(cy)} L{_n(x + 1)} {_n(cy - 5)} L{_n(x + 1)} {_n(cy + 5)} Z"/>'
        f'<rect class="bubble-card" x="{_n(x)}" y="{_n(y0)}" width="{_n(w)}" height="40" rx="10" '
        f'filter="url(#{prefix}-pill-sh)"/>'
        f'<rect class="bubble-bar bar-{arm}" x="{_n(x + 6)}" y="{_n(y0 + 10)}" width="3" height="20" rx="1.5"/>'
        + _t(x + 16, cy + 5, quoted, "quote-t", "start")
        + "</g>"
    )


def _pill_btn(x: float, y: float, w: float, label: str, attrs: str, prefix: str) -> str:
    """画布内轻量胶囊交互按钮（替代旧版纯黑直角实心块）。"""
    return (
        f'<g class="sbtn clickable" {attrs} role="button" tabindex="0">'
        f'<rect class="sbtn-bg" x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="32" rx="16" '
        f'filter="url(#{prefix}-pill-sh)"/>'
        + _t(x + w / 2.0, y + 20.5, label, "sbtn-t", "middle")
        + "</g>"
    )


# ------------------------------------------------------------ 第 1 幕：克隆平行沙盘
def _svg_clone(p: dict) -> str:
    c, scale = p["clone"], p["scale"]
    pfx = "s0"
    parts = [
        '<svg class="sandbox" id="clone-stage" viewBox="0 0 1240 572" preserveAspectRatio="xMidYMid meet" '
        'role="img" aria-label="克隆平行沙盘：同质培养皿、父快照克隆枢纽、三座实验舱与等证据剂量天平">',
        _svg_defs(pfx),
    ]
    # 左卡：同质信息培养皿 (x=24..392, y=14..554)
    parts.append(f'<rect class="panel-card" x="24" y="14" width="368" height="540" rx="18" filter="url(#{pfx}-shadow)"/>')
    parts.append(_t(208, 46, "同质信息培养皿", "card-h", "middle"))
    parts.append(_t(208, 67, "8 轮连续曝光 · 同阵营回声与失真数字交织", "lbl-xs", "middle"))
    parts.append('<circle class="dish-outer" cx="208" cy="218" r="118"/>'
                 '<circle class="dish-mid" cx="208" cy="218" r="88"/>'
                 '<circle class="dish-inner" cx="208" cy="218" r="56"/>')
    outer_pts, inner_pts = [], []
    for i, kind in enumerate(c["particles"]):
        ang = math.radians(i * 15 - 90)
        rad = 106 if i % 2 == 0 else 74
        x, y = round(208 + rad * math.cos(ang), 1), round(218 + rad * math.sin(ang), 1)
        node = f'<circle class="pt-{kind}" cx="{_n(x)}" cy="{_n(y)}" r="{6.4 if kind == "both" else 5.4}"/>'
        if i % 2 == 0:
            outer_pts.append(node)
        else:
            inner_pts.append(node)
    parts.append('<g class="orbit-cw">' + "".join(outer_pts) + "</g>")
    parts.append('<g class="orbit-ccw">' + "".join(inner_pts) + "</g>")
    parts.append(_agent_node(208, 218, 1.12, "neutral", pfx))

    # 左卡底部内嵌维恩配额面板
    v = c["venn"]
    parts.append('<g class="clickable" data-open="boundary" role="button" tabindex="0">'
                 '<rect class="inset-box" x="44" y="368" width="328" height="166" rx="12"/>'
                 '<circle class="venn-a" cx="112" cy="442" r="38"/>'
                 '<circle class="venn-b" cx="154" cy="442" r="31"/>'
                 + _t(98, 446, str(v["same"]), "num-b", "middle")
                 + _t(166, 446, str(v["rumor"]), "num-b t-red", "middle")
                 + '<circle class="pt-same" cx="212" cy="414" r="4.5"/>'
                 + _t(224, 418, "同阵营帖", "lbl-xs") + _t(352, 418, str(v["same"]), "mono-b", "end")
                 + '<circle class="pt-rumor" cx="212" cy="442" r="4.5"/>'
                 + _t(224, 446, "含失真数字", "lbl-xs t-red") + _t(352, 446, str(v["rumor"]), "mono-b t-red", "end")
                 + '<circle class="pt-both" cx="212" cy="470" r="5"/>'
                 + _t(224, 474, "两类属性重叠", "lbl-xs") + _t(352, 474, str(v["overlap"]), "mono-b", "end")
                 + _t(208, 514, f"合计 {v['total']} 次曝光（2 人 × 48 张）· 非互斥配额", "lbl-xs", "middle")
                 + "</g>")

    # 中列：T1 父快照克隆枢纽 + 实验规模底账卡
    parts.append('<path class="conduit-in" d="M392 218 L466 218"/>'
                 '<rect class="chip-bg" x="398" y="191" width="64" height="20" rx="10"/>'
                 + _t(430, 205, "刷满 8 轮", "lbl-xxs", "middle"))
    s01 = next(s for s in p["subjects"] if s["subject_id"] == "S01")
    parts.append('<g class="gate clickable" data-act="clone" role="button" tabindex="0">'
                 f'<circle cx="530" cy="218" r="70" fill="url(#{pfx}-halo-amber)"/>'
                 '<circle class="gate-outer" cx="530" cy="218" r="60"/>'
                 f'<circle class="gate-core" cx="530" cy="218" r="50" filter="url(#{pfx}-shadow)"/>'
                 + _t(530, 209, "T1 父快照", "lbl-b", "middle")
                 + '<rect class="hash-pill" x="483" y="218" width="94" height="20" rx="10"/>'
                 + _t(530, 232, s01["parent_snapshot_sha256"][:12], "mono-xs", "middle")
                 + _t(530, 300, "状态哈希锁定 · 点击重播分流", "lbl-xs", "middle")
                 + "</g>")

    # 中列底部：实验规模底账卡（与左右两列底边 y=554 严格齐平）
    parts.append('<g class="clickable" data-open="boundary" role="button" tabindex="0">'
                 f'<rect class="panel-card" x="416" y="368" width="228" height="186" rx="16" filter="url(#{pfx}-shadow)"/>'
                 + _t(436, 398, "实验规模底账", "card-h-sm")
                 + '<line class="hairline" x1="436" y1="410" x2="624" y2="410"/>'
                 + _t(436, 442, f"{scale['topics']}", "kpi-num") + _t(436, 460, "实测议题", "lbl-xs")
                 + _t(536, 442, f"{scale['final_run_calls']}", "kpi-num") + _t(536, 460, "最终运行调用", "lbl-xs")
                 + _t(436, 504, f"{scale['answers_and_blind_evaluations']}", "kpi-num") + _t(436, 522, "答卷与双盲评", "lbl-xs")
                 + _t(536, 504, f"{scale['archived_tests']}", "kpi-num") + _t(536, 522, "归档回归项", "lbl-xs")
                 + "</g>")

    # 右列：三座平行实验舱 + 底部等证据公平性天平卡 (x=672..1216, y=14..554)
    lanes = {"I0": 64, "I5": 182, "I4": 318}
    for arm, y in lanes.items():
        parts.append(f'<path class="branch b-{arm}" d="M588 218 C632 218 628 {y} 672 {y}"/>')
    chambers = (
        ("I0", 14, 100, "继续刷同温层", "同质讨论回放 · 无权威核查证据", "muted"),
        ("I5", 132, 100, "官方原句摘录", "100% 共享证据块 + 算法确定性整句摘录", "teal"),
        ("I4", 250, 140, "破茧助手完整介入", "100% 共享证据块 + 个性化口径纠偏与反思追问", "amber"),
    )
    for arm, y0, h, title, desc, tone in chambers:
        ym = y0 + 50
        parts.append(f'<g class="chamber-g clickable" data-open="source" role="button" tabindex="0">'
                     f'<rect class="panel-card ch-{arm}" x="672" y="{y0}" width="544" height="{h}" rx="16" '
                     f'filter="url(#{pfx}-shadow)"/>'
                     f'<rect class="ch-bar acc-{arm}" x="684" y="{y0 + 18}" width="4" height="{h - 36}" rx="2"/>'
                     f'<rect class="arm-chip chip-{arm}" x="702" y="{y0 + 18}" width="44" height="26" rx="8"/>'
                     + _t(724, y0 + 36, arm, f"arm-chip-t t-{arm}", "middle")
                     + _t(758, y0 + 37, title, "card-h-sm")
                     + _t(702, y0 + 68, desc, "lbl-s"))
        if arm == "I4":
            steps = ("01 检索", "02 贴标", "03 扫盲", "04 整合", "05 认知镜")
            for k, step in enumerate(steps):
                sx = 702 + k * 84
                parts.append(f'<rect class="step-pill" x="{sx}" y="{y0 + 88}" width="72" height="28" rx="8"/>'
                             + _t(sx + 36, y0 + 106, step, "lbl-xxs t-I4", "middle"))
                if k < 4:
                    parts.append(f'<line class="step-link" x1="{sx + 74}" y1="{y0 + 102}" x2="{sx + 82}" y2="{y0 + 102}"/>')
        parts.append(_agent_node(1168, ym, 0.72, tone, pfx))
        parts.append("</g>")

    # 克隆分流动态粒子（默认隐藏，播放时沿贝塞尔流线平滑滑入三舱）
    for arm, y in lanes.items():
        dy = y - 218
        parts.append(f'<g transform="translate(588 218)"><circle class="token tk-{arm}" r="7.5" opacity="0">'
                     f'<animateMotion class="smil" dur="1.4s" begin="indefinite" fill="freeze" '
                     f'calcMode="spline" keyTimes="0;1" keySplines="0.22 1 0.36 1" '
                     f'path="M0 0 C44 0 40 {dy} 84 {dy}"/>'
                     '<animate class="smil" attributeName="opacity" values="0;1;1;0" keyTimes="0;0.1;0.85;1" '
                     'dur="1.4s" begin="indefinite" fill="freeze"/></circle></g>')

    # 右列底部：等证据剂量精密天平卡 (y=408..554)
    tilt = max(-5.0, min(5.0, (c["i4_mean_chars"] - c["i5_mean_chars"]) / c["i5_mean_chars"] * 100 * 0.8))
    parts.append('<g class="balance clickable" data-open="source" role="button" tabindex="0">'
                 f'<rect class="panel-card" x="672" y="408" width="544" height="146" rx="16" filter="url(#{pfx}-shadow)"/>'
                 # 左侧精密实验室天平图形 (cx=748)
                 '<line class="bal-base" x1="714" y1="524" x2="782" y2="524"/>'
                 '<line class="bal-pillar" x1="748" y1="454" x2="748" y2="524"/>'
                 '<circle class="bal-dial" cx="748" cy="454" r="6"/>'
                 f'<g transform="rotate({_n(round(tilt, 2))} 748 454)">'
                 '<line class="bal-beam" x1="702" y1="454" x2="794" y2="454"/>'
                 '<path class="bal-cord" d="M702 454 L692 486 M702 454 L712 486 M794 454 L784 486 M794 454 L804 486"/>'
                 '<path class="bal-pan pan-i5" d="M688 486 Q702 494 716 486"/>'
                 '<path class="bal-pan pan-i4" d="M780 486 Q794 494 808 486"/>'
                 "</g>"
                 # 右侧读数
                 + _t(832, 442, "公平性天平 · 共享 100% 同一证据块", "card-h-sm")
                 + '<rect class="ok-pill" x="1086" y="424" width="112" height="24" rx="12"/>'
                 + _t(1142, 440, f"七项检查 {c['pair_rounds_passed']}/{c['pair_rounds_passed']} 通过", "ok-pill-t", "middle")
                 + _t(832, 476, f"I5 均 {c['i5_mean_chars']:g} 字", "mono-b t-I5")
                 + _t(946, 476, "vs", "lbl-xs")
                 + _t(972, 476, f"I4 均 {c['i4_mean_chars']:g} 字", "mono-b t-I4")
                 + _t(1090, 476, f"最大差 {c['max_difference_pct']:g}%（≤{c['tolerance_pct']}%）", "lbl-xs")
                 + _t(832, 506, f"可见全文 {c['char_min']}–{c['char_max']} 字，严格落入 {c['dose_window'][0]}–{c['dose_window'][1]} 字预设剂量窗口", "lbl-xs")
                 + _t(832, 530, "点击天平或上方实验舱检视逐轮配长表与原始文本", "lbl-xxs")
                 + "</g>")
    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------ 第 2 幕：茧房染色现场
def _svg_dye(p: dict) -> str:
    pfx = "s1"
    parts = [
        '<svg class="sandbox" id="dye-stage" viewBox="0 0 1240 572" preserveAspectRatio="xMidYMid meet" '
        'role="img" aria-label="茧房染色现场：48 张帖子晶格矩阵、受试表达与测量双层剖面、光学认知镜诊断">',
        _svg_defs(pfx),
    ]
    avatar_xy = (620, 284)
    for s in p["subjects"]:
        sid = s["subject_id"]
        parts.append(f'<g class="dye" id="dye-{sid}" data-only-subject="{sid}">')

        # 左卡：8 轮 × 6 张信息流晶格矩阵 (x=24..416, y=14..554)
        parts.append(f'<rect class="panel-card" x="24" y="14" width="392" height="540" rx="18" filter="url(#{pfx}-shadow)"/>')
        parts.append(_t(48, 46, "8 轮 × 6 张曝光晶格", "card-h"))
        parts.append(_t(48, 67, "悬停任一晶格检视原帖与核查证据旁注", "lbl-xs"))
        for r in range(1, 9):
            cx = 68 + (r - 1) * 43
            parts.append(_t(cx, 100, f"R{r}", "mono-xs", "middle"))

        pulses = []
        for idx, card in enumerate(s["feed"]):
            cx = 68 + (card["round"] - 1) * 43
            cy = 130 + (card["slot"] - 1) * 44
            cls = f"beacon r{card['round']}" + (" rumor" if card["misleading"] else "")
            if card["relation"] == "same":
                tile = f'<rect class="bc-same" x="{cx - 10}" y="{cy - 10}" width="20" height="20" rx="5.5"/>'
            elif card["relation"] == "neutral":
                tile = f'<rect class="bc-neutral" x="{cx - 9.5}" y="{cy - 9.5}" width="19" height="19" rx="5"/>'
            else:
                tile = f'<circle class="bc-opp" cx="{cx}" cy="{cy}" r="9"/>'
            halo = (f'<circle class="rumor-ring" cx="{cx}" cy="{cy}" r="14.5"/>'
                    f'<circle class="rumor-dot" cx="{cx + 8}" cy="{cy - 8}" r="3.2"/>') if card["misleading"] else ""
            parts.append(f'<g class="{cls}" data-card="{sid}:{idx}" tabindex="0">{halo}{tile}</g>')
            if card["misleading"]:
                qx = round((cx + avatar_xy[0]) * 0.52, 1)
                qy = round(cy * 0.65 + avatar_xy[1] * 0.35 - 26, 1)
                pulses.append(f'<path class="pulse r{card["round"]}" '
                              f'd="M{cx} {cy} Q{_n(qx)} {_n(qy)} {avatar_xy[0] - 28} {avatar_xy[1]}"/>')

        # 左卡底部图例
        e = s["exposure"]
        parts.append('<rect class="inset-box" x="44" y="388" width="352" height="146" rx="12"/>'
                     '<rect class="bc-same" x="62" y="408" width="14" height="14" rx="4"/>'
                     + _t(84, 420, f"同阵营 {e['same']}", "lbl-xs")
                     + '<rect class="bc-neutral" x="182" y="408" width="14" height="14" rx="4"/>'
                     + _t(204, 420, f"中立 {e['neutral']}", "lbl-xs")
                     + '<circle class="bc-opp" cx="296" cy="415" r="6.5"/>'
                     + _t(310, 420, f"对立 {e['opposite']}", "lbl-xs")
                     + '<circle class="rumor-ring" cx="69" cy="452" r="8"/>'
                     + '<circle class="rumor-dot" cx="75" cy="446" r="2.8"/>'
                     + _t(86, 456, f"含失真数字 {e['rumor']} 张（与同阵营重叠 {e['overlap']} 张）", "lbl-xs t-red")
                     + _t(62, 488, "注：绿色核查旁注仅供演示对照，曝光阶段受试不可见", "lbl-xxs")
                     + _t(62, 512, f"后续保持期另回流 {e['retention_rumor']} 张失真帖检验抗回退", "lbl-xxs"))

        # 中卡：受试「表达 vs 测量」双层剖面舱 (x=436..804, y=14..554)
        parts.append(f'<rect class="panel-card" x="436" y="14" width="368" height="540" rx="18" filter="url(#{pfx}-shadow)"/>')
        # 脉冲弧线叠在卡片之上、气泡与受试之下
        parts.append('<g class="pulses">' + "".join(pulses) + "</g>")
        # 上半：表达层暖红柔光区
        parts.append('<rect class="zone-exp" x="454" y="30" width="332" height="206" rx="13"/>'
                     + _t(620, 54, "表达层 · 发帖转述的网传数字（原话）", "lbl-xs t-red", "middle"))
        for k, text in enumerate(s["bubbles"]):
            parts.append(_callout_pill(620, 68 + k * 44, text, pfx, "red", 13.0))
        parts.append('<line class="stem-red" x1="620" y1="202" x2="620" y2="250"/>')

        # 中央受试认知核
        parts.append('<g class="clickable" data-open="source" role="button" tabindex="0">'
                     + _agent_node(620, 284, 1.18, "red", pfx) + "</g>")
        parts.append('<line class="stem-teal" x1="620" y1="318" x2="620" y2="342"/>')

        # 下半：答卷层精密半圆刻度盘
        parts.append('<rect class="zone-meas" x="454" y="340" width="332" height="198" rx="13"/>'
                     + _t(620, 366, "答卷层 · T1 正式概率问卷测量", "lbl-xs", "middle"))
        gcx, gcy, gr = 620, 468, 62
        # 中立审慎扇区高亮 (0.42..0.58)
        parts.append(f'<path class="gauge-safe" d="M{_n(gcx - 16)} {_n(gcy - 59.9)} A{gr} {gr} 0 0 1 {_n(gcx + 16)} {_n(gcy - 59.9)} L{gcx} {gcy} Z"/>')
        parts.append(f'<path class="gauge-track" d="M{gcx - gr} {gcy} A{gr} {gr} 0 0 1 {gcx + gr} {gcy}"/>')
        for step in range(11):
            ang = math.pi * (1.0 - step / 10.0)
            r_in = gr - (10 if step % 5 == 0 else 6)
            x1, y1 = gcx + r_in * math.cos(ang), gcy - r_in * math.sin(ang)
            x2, y2 = gcx + gr * math.cos(ang), gcy - gr * math.sin(ang)
            parts.append(f'<line class="gauge-tick" x1="{_n(round(x1, 1))}" y1="{_n(round(y1, 1))}" '
                         f'x2="{_n(round(x2, 1))}" y2="{_n(round(y2, 1))}"/>')
        parts.append(f'<line class="needle" x1="{gcx}" y1="{gcy}" x2="{gcx}" y2="{gcy - gr + 10}"/>'
                     f'<circle class="needle-hub" cx="{gcx}" cy="{gcy}" r="6.5"/>'
                     f'<circle class="needle-core" cx="{gcx}" cy="{gcy}" r="2.5"/>'
                     + _t(gcx - gr - 6, gcy + 14, "0.0", "mono-xs", "middle")
                     + _t(gcx, gcy - gr - 10, "0.50", "mono-b t-I5", "middle")
                     + _t(gcx + gr + 6, gcy + 14, "1.0", "mono-xs", "middle")
                     + _t(620, 504, f"{s['t1_items_at_half']}/{s['t1_items']} 题均填 p_true = 0.50", "lbl-b", "middle")
                     + _t(620, 525, "未见权威核查即保持“拿不准”审慎中立", "lbl-xxs", "middle"))

        # 右卡：自我认知镜光学诊断舱 (x=824..1216, y=14..554)
        m = s["mirror"]
        parts.append(f'<rect class="panel-card" x="824" y="14" width="392" height="540" rx="18" filter="url(#{pfx}-shadow)"/>')
        parts.append('<line class="scan-beam" x1="654" y1="284" x2="844" y2="284"/>')
        parts.append(_optical_lens(862, 284, 92.0, pfx, "认知镜"))

        # 右卡上部：立场光谱
        parts.append(_t(910, 46, "立场光谱定位（镜像诊断）", "card-h-sm"))
        sx0, sw = 910, 278
        mx = round(sx0 + sw * (m["stance_pct"] / 100.0), 1)
        parts.append(f'<rect x="{sx0}" y="102" width="{sw}" height="8" rx="4" fill="url(#{pfx}-spec)"/>')
        for k in range(5):
            tx = round(sx0 + sw * k / 4.0, 1)
            parts.append(f'<line class="spec-tick" x1="{_n(tx)}" y1="98" x2="{_n(tx)}" y2="114"/>')
        parts.append(f'<rect class="spec-pill" x="{_n(mx - 24)}" y="66" width="48" height="22" rx="11"/>'
                     f'<path class="spec-ptr" d="M{_n(mx)} 98 L{_n(mx - 5)} 89 L{_n(mx + 5)} 89 Z"/>'
                     + _t(mx, 81, f"{m['stance_pct']}%", "mono-b t-I4", "middle")
                     + _t(sx0, 130, "0% 挺乙（智轨）", "lbl-xxs")
                     + _t(sx0 + sw, 130, "100% 挺甲（公交）", "lbl-xxs", "end"))
        parts.append(f'<path class="fiber" d="M878 262 C896 248 894 106 {sx0} 106"/>')

        # 右卡中下部：5 条无交叉横向光学诊断条目卡（彻底杜绝旧版同心圆靶压字问题）
        biases = [t for t in m["targets"] if t["kind"] == "bias"]
        blinds = [t for t in m["targets"] if t["kind"] == "blindspot"]
        parts.append(_t(910, 168, "思维偏差识别（点击检视诊断与引句）", "lbl-xs t-red"))
        for k, target in enumerate(biases):
            cy = 200 + k * 50
            parts.append(f'<path class="fiber" d="M880 284 C896 284 892 {cy} 910 {cy}"/>'
                         f'<g class="diag-row clickable" data-mirror="{sid}-{target["index"]}" role="button" tabindex="0">'
                         f'<rect class="diag-card" x="910" y="{cy - 20}" width="282" height="40" rx="10"/>'
                         f'<circle class="diag-dot dot-bias" cx="928" cy="{cy}" r="5"/>'
                         + _t(944, cy + 5, target["label"], "lbl-s")
                         + _t(1176, cy + 5, "引句 ›", "lbl-xxs", "end")
                         + "</g>")

        parts.append(_t(910, 366, "关键证据盲区扫描", "lbl-xs t-I5"))
        for k, target in enumerate(blinds):
            cy = 398 + k * 50
            parts.append(f'<path class="fiber fiber-teal" d="M880 296 C896 296 892 {cy} 910 {cy}"/>'
                         f'<g class="diag-row clickable" data-mirror="{sid}-{target["index"]}" role="button" tabindex="0">'
                         f'<rect class="diag-card" x="910" y="{cy - 20}" width="282" height="40" rx="10"/>'
                         f'<circle class="diag-dot dot-blind" cx="928" cy="{cy}" r="5"/>'
                         + _t(944, cy + 5, target["label"], "lbl-s")
                         + _t(1176, cy + 5, "详情 ›", "lbl-xxs", "end")
                         + "</g>")
        parts.append(_t(1051, 520, "点击任一诊断条滑出 mirror 全文与原话锚点", "lbl-xxs", "middle"))
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------ 第 3 幕：三条平行轨道对决
def _ring(cx: float, cy: float, r: float, pct: float, cls: str) -> str:
    circ = 2.0 * math.pi * r
    on = circ * max(0.0, min(100.0, pct)) / 100.0
    return (f'<circle class="ring-bg" cx="{_n(cx)}" cy="{_n(cy)}" r="{_n(r)}"/>'
            f'<circle class="ring-val {cls}" cx="{_n(cx)}" cy="{_n(cy)}" r="{_n(r)}" '
            f'stroke-dasharray="{_n(round(on, 1))} {_n(round(circ, 1))}" transform="rotate(-90 {_n(cx)} {_n(cy)})"/>')


def _svg_tracks(p: dict) -> str:
    pfx = "s2"
    parts = [
        '<svg class="sandbox" id="track-stage" viewBox="0 0 1240 572" preserveAspectRatio="xMidYMid meet" '
        'role="img" aria-label="三条平行泳道：同一父快照分叉出的 I0、I5、I4 干预信号、原话回应与 T3 终局">',
        _svg_defs(pfx),
    ]
    lanes = (("I0", 18, 154, "muted"), ("I5", 206, 154, "teal"), ("I4", 394, 162, "amber"))
    for s in p["subjects"]:
        sid = s["subject_id"]
        for r_key, rc in s["rounds"].items():
            parts.append(f'<g class="tracks" data-only-subject="{sid}" data-only-round="{r_key}">')

            # 左侧父快照分叉晶核 (cx=86, cy=284)
            parts.append(f'<circle cx="86" cy="284" r="68" fill="url(#{pfx}-halo-teal)"/>'
                         '<circle class="gate-outer" cx="86" cy="284" r="58"/>'
                         f'<circle class="gate-core" cx="86" cy="284" r="49" filter="url(#{pfx}-shadow)"/>'
                         + _t(86, 274, "T1 父快照", "lbl-b", "middle")
                         + '<rect class="hash-pill" x="39" y="284" width="94" height="19" rx="9.5"/>'
                         + _t(86, 297, s["parent_snapshot_sha256"][:12], "mono-xs", "middle")
                         + _t(86, 360, "三轨同源哈希", "lbl-xs", "middle")
                         + _t(86, 380, f"{sid} · 第 {r_key} 轮", "mono-xs", "middle"))

            # 先绘制三条分叉流线与泳道卡
            for arm, y0, h, tone in lanes:
                cy = y0 + 77
                parts.append(f'<path class="branch b-{arm}" d="M140 284 C168 284 162 {cy} 188 {cy}"/>')
                parts.append(f'<g class="track clickable" data-open="source" role="button" tabindex="0">'
                             f'<rect class="panel-card lane-{arm}" x="188" y="{y0}" width="1028" height="{h}" rx="16" '
                             f'filter="url(#{pfx}-shadow)"/>'
                             f'<rect class="ch-bar acc-{arm}" x="200" y="{y0 + 18}" width="4" height="{h - 36}" rx="2"/>')

                # 第 1 列 (x=214..346)：臂标识与配长元数据（绝不与第 2 列共享证据连桥交叉）
                parts.append(f'<rect class="arm-chip chip-{arm}" x="214" y="{y0 + 20}" width="42" height="24" rx="7"/>'
                             + _t(235, y0 + 36, arm, f"arm-chip-t t-{arm}", "middle")
                             + _t(264, y0 + 37, {"I0": "同温层回放", "I5": "官方原句", "I4": "破茧助手"}[arm], "card-h-sm"))
                if arm == "I0":
                    parts.append(_t(214, y0 + 68, f"可见 {rc['i0_chars']} 字", "mono-xs")
                                 + _t(214, y0 + 90, "无权威核查证据", "lbl-xxs"))
                elif arm == "I5":
                    parts.append(_t(214, y0 + 68, f"可见 {rc['i5_chars']} 字", "mono-xs t-I5")
                                 + _t(214, y0 + 90, "算法确定性整句", "lbl-xxs"))
                else:
                    parts.append(_t(214, y0 + 68, f"可见 {rc['i4_chars']} 字", "mono-xs t-I4")
                                 + _t(214, y0 + 90, "七项配对门禁全过", "lbl-xxs"))
                    if rc["review_flag"]:
                        parts.append(f'<rect class="flag-pill" x="214" y="{y0 + 108}" width="126" height="22" rx="6"/>'
                                     + _t(277, y0 + 123, "1 处引用错位待复核", "flag-pill-t", "middle"))

                # 第 2 列 (x=358..604)：干预机制图形
                if arm == "I0":
                    wave = f"M366 {cy} " + " ".join(f"q14 {-11 if k % 2 == 0 else 11} 28 0" for k in range(7))
                    parts.append(f'<path class="wave" d="{wave}"/>'
                                 f'<line class="flow-muted" x1="562" y1="{cy}" x2="606" y2="{cy}"/>'
                                 + _t(478, cy + 32, "论坛同阵营旧帖回放", "lbl-xxs", "middle"))
                elif arm == "I5":
                    parts.append(f'<rect class="ev-box" x="360" y="{cy - 20}" width="102" height="40" rx="10"/>'
                                 + _t(411, cy + 5, "共享证据块", "lbl-xs t-green", "middle")
                                 + f'<line class="sig sig-I5" x1="462" y1="{cy}" x2="484" y2="{cy}"/>'
                                 + f'<rect class="ex-box" x="484" y="{cy - 20}" width="98" height="40" rx="10"/>'
                                 + _t(533, cy + 5, "整句摘录器", "lbl-xs t-I5", "middle")
                                 + f'<line class="sig sig-I5" x1="582" y1="{cy}" x2="608" y2="{cy}"/>')
                else:
                    parts.append(f'<rect class="ev-box" x="360" y="{cy - 20}" width="102" height="40" rx="10"/>'
                                 + _t(411, cy + 5, "共享证据块", "lbl-xs t-green", "middle")
                                 + f'<line class="sig" x1="462" y1="{cy}" x2="500" y2="{cy}"/>'
                                 + _optical_lens(516, cy, 62.0, pfx, "")
                                 + _t(516, cy + 48, "认知镜", "lbl-xxs t-I4", "middle")
                                 + f'<path class="sig" d="M532 {cy - 8} L608 {cy - 14}"/>'
                                 + f'<path class="sig" d="M532 {cy + 8} L608 {cy + 14}"/>'
                                 + _t(572, cy - 22, "点破误区", "lbl-xxs t-I4", "middle")
                                 + _t(572, cy + 31, f"追问 ×{rc['i4_questions']}", "lbl-xxs t-I4", "middle"))

                # 第 3 列 (x=616..1026)：受试认知核 + 右侧紧邻原话回应气泡
                badge = "?" if arm == "I0" else "ok"
                parts.append(_agent_node(638, cy, 0.86, tone, pfx, badge))
                parts.append(_track_quote_card(676, cy, 344.0, rc["bubbles"][arm], pfx, arm))

                # 第 4 列 (x=1036..1202)：T3 终局仪表环 + 迁移格 + U 指标
                t3 = s["t3"][arm]
                parts.append(f'<line class="hairline" x1="1034" y1="{y0 + 22}" x2="1034" y2="{y0 + h - 22}"/>')
                parts.append(_ring(1070, cy, 21, t3["Acc"], f"rv-{arm}")
                             + _t(1070, cy + 4, f"{t3['Acc']:g}%", "num-xs", "middle")
                             + _t(1070, cy + 38, "核心事实", "lbl-xxs", "middle"))
                for k in range(t3["transfer_total"]):
                    on = "cell-on c-" + arm if k < t3["transfer_correct"] else "cell-off"
                    parts.append(f'<rect class="{on}" x="{1108 + k * 19}" y="{cy - 18}" width="14" height="14" rx="3.5"/>')
                parts.append(_t(1108, cy + 14, f"迁移 {t3['transfer_correct']}/{t3['transfer_total']}", "lbl-xxs")
                             + _t(1108, cy + 34, f"换位 U {t3['U']:g}", "mono-xs"))
                parts.append("</g>")

                # 轨道干预脉冲光点动画
                parts.append(f'<g transform="translate(360 {cy})"><circle class="token tk-{arm}" r="6.5" opacity="0">'
                             '<animateMotion class="smil" dur="1.25s" begin="indefinite" fill="freeze" '
                             'calcMode="spline" keyTimes="0;1" keySplines="0.22 1 0.36 1" path="M0 0 L252 0"/>'
                             '<animate class="smil" attributeName="opacity" values="0;1;1;0" keyTimes="0;0.1;0.85;1" '
                             'dur="1.25s" begin="indefinite" fill="freeze"/></circle></g>')

            # 跨泳道共享证据桥接舱（精确穿过 I5 与 I4 泳道间隙 y=360..394，零遮挡任何文字）
            parts.append('<line class="bridge-line" x1="411" y1="303" x2="411" y2="451"/>'
                         f'<rect class="bridge-pill" x="355" y="366" width="112" height="22" rx="11" '
                         f'filter="url(#{pfx}-pill-sh)"/>'
                         + _t(411, 381, "100% 同一证据块", "bridge-t", "middle"))
            parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------ 第 4 幕：双轨终局与防伪账本
def _chart(x0: int, y0: int, w: int, h: int, track: dict, metric: str, synth: bool, pfx: str) -> str:
    px0, px1, py0, py1 = x0 + 62, x0 + w - 34, y0 + 78, y0 + h - 38
    ymax = 100.0 if metric == "acc" else 0.30
    series = track["acc_series"] if metric == "acc" else track["bs_series"]

    def X(i: int) -> float:
        return round(px0 + (px1 - px0) * i / 3.0, 1)

    def Y(v: float) -> float:
        return round(py1 - (py1 - py0) * v / ymax, 1)

    out = [f'<rect class="panel-card" x="{x0}" y="{y0}" width="{w}" height="{h}" rx="18" filter="url(#{pfx}-shadow)"/>']
    title = ("现实三议题 · 先验天花板" if not synth else "星澜市封闭新题 · 零先验破局")
    badge = (f"{track['branches']} 分支 · {track['accepted_calls']} 次采纳" if not synth
             else f"零先验 · {track['calls']} 次调用")
    out.append(_t(x0 + 24, y0 + 34, title, "card-h-sm")
               + f'<rect class="chip-bg" x="{x0 + w - 166}" y="{y0 + 17}" width="142" height="24" rx="12"/>'
               + _t(x0 + w - 95, y0 + 33, badge, "lbl-xxs", "middle"))

    ticks = (0, 50, 100) if metric == "acc" else (0.0, 0.1, 0.2, 0.3)
    for tv in ticks:
        yv = Y(tv)
        out.append(f'<line class="grid" x1="{px0}" y1="{_n(yv)}" x2="{px1}" y2="{_n(yv)}"/>'
                   + _t(px0 - 10, yv + 4, (f"{tv:g}%" if metric == "acc" else f"{tv:.1f}"), "mono-xs", "end"))
    for i, tp in enumerate(("T0 基线", "T1 刷帖后", "T2 干预后", "T3 回流后")):
        anchor = "start" if i == 0 else ("end" if i == 3 else "middle")
        out.append(_t(X(i), py1 + 22, tp, "mono-xs", anchor))

    if synth:
        # 4 轮误导帖回流柔和珊瑚色带（标签胶囊置于顶部网格线之上，与 100% 曲线文字彻底分层零碰撞）
        rx_w = round(X(3) - X(2), 1)
        rcx = round((X(2) + X(3)) / 2.0, 1)
        out.append(f'<rect class="reflux" x="{_n(X(2))}" y="{py0}" width="{_n(rx_w)}" height="{py1 - py0}" rx="6"/>'
                   + f'<rect class="flag-pill" x="{_n(rcx - 58)}" y="{py0 - 25}" width="116" height="19" rx="9.5"/>'
                   + _t(rcx, py0 - 12, f"{track['reflux_rounds']} 轮误导回流区", "flag-pill-t", "middle"))
        gv = 50.0 if metric == "acc" else 0.25
        out.append(f'<line class="guess" x1="{px0}" y1="{_n(Y(gv))}" x2="{px1}" y2="{_n(Y(gv))}"/>'
                   + _t(px0 + 6, Y(gv) - 8, "50% 盲猜基准线" if metric == "acc" else "0.25 盲猜基准线", "lbl-xxs"))
        area_pts = " ".join(f"{_n(X(i))},{_n(Y(v))}" for i, v in enumerate(series["I4"]))
        if metric == "acc":
            out.append(f'<polygon fill="url(#{pfx}-area-amber)" points="{_n(X(0))},{py1} {area_pts} {_n(X(3))},{py1}"/>')
    elif metric == "acc":
        out.append(f'<rect class="ceiling-band" x="{px0}" y="{_n(Y(100) - 25)}" width="244" height="19" rx="9.5"/>'
                   + _t(px0 + 122, Y(100) - 12, "预训练先验满分天花板（实验前即 100%）", "lbl-xxs t-I4", "middle"))

    # 平行微偏移：防止重合线段混色成泥灰绿，保持三色各自清透可辨
    offsets = {"I0": 0.0, "I5": -1.8, "I4": 1.8} if synth else {"I0": 2.4, "I5": 0.0, "I4": -2.4}
    for arm in ARMS:
        dy = offsets[arm]
        pts = " ".join(f"{_n(X(i))},{_n(round(Y(v) + dy, 1))}" for i, v in enumerate(series[arm]))
        out.append(f'<polyline class="ln ln-{arm} draw" points="{pts}"/>')

    # 数据节点：重合处采用同心双色靶环（外青环 + 内琥珀核）
    for i in range(4):
        v0, v5, v4 = series["I0"][i], series["I5"][i], series["I4"][i]
        fmtv = (lambda v: f"{v:g}%") if metric == "acc" else (lambda v: f"{v:.4f}")
        out.append(f'<circle class="pt pt-I0" cx="{_n(X(i))}" cy="{_n(round(Y(v0) + offsets["I0"], 1))}" r="4.2" '
                   f'data-tip="{_e("I0 · T" + str(i) + " · " + fmtv(v0))}"/>')
        if synth and v5 == v4:
            tip = f"I5 = I4 · T{i} · {fmtv(v4)}"
            out.append(f'<g data-tip="{_e(tip)}" class="dual-pt">'
                       f'<circle class="pt-dual-out" cx="{_n(X(i))}" cy="{_n(Y(v4))}" r="6.2"/>'
                       f'<circle class="pt-dual-in" cx="{_n(X(i))}" cy="{_n(Y(v4))}" r="3.1"/></g>')
        else:
            for arm in ("I5", "I4"):
                vv = series[arm][i]
                out.append(f'<circle class="pt pt-{arm}" cx="{_n(X(i))}" cy="{_n(round(Y(vv) + offsets[arm], 1))}" r="4.4" '
                           f'data-tip="{_e(arm + " · T" + str(i) + " · " + fmtv(vv))}"/>')

    last = {a: series[a][3] for a in ARMS}
    fmtv = (lambda v: f"{v:g}%") if metric == "acc" else (lambda v: f"{v:.4f}")
    if synth:
        if last["I4"] == last["I5"]:
            ly = Y(last["I4"]) + (22 if metric == "acc" else -13)
            out.append(_t(X(3) - 8, ly, f"I5 = I4 · {fmtv(last['I4'])}（打平）", "lbl-xs t-I4", "end"))
        out.append(_t(X(3) - 8, Y(last["I0"]) + 18, f"I0 · {fmtv(last['I0'])}", "lbl-xs", "end"))
    else:
        ly = Y(last["I4"]) + (22 if metric == "acc" else -14)
        note = f"三臂贴顶重合 {fmtv(last['I4'])}" if metric == "acc" else f"基线误差已近 0 · 约 {fmtv(last['I4'])}"
        out.append(_t(X(3) - 8, ly, note, "lbl-xs", "end"))
    return "".join(out)


def _svg_final(p: dict) -> str:
    pfx = "s3"
    parts = [
        '<svg class="sandbox" id="final-stage" viewBox="0 0 1240 572" preserveAspectRatio="xMidYMid meet" '
        'role="img" aria-label="双轨终局：现实议题与封闭新题四时点曲线，以及四项终局指标胶囊哑铃图">',
        _svg_defs(pfx),
    ]
    for metric in ("acc", "brier"):
        parts.append(f'<g data-only-metric="{metric}">'
                     + _chart(24, 12, 582, 286, p["real_track"], metric, False, pfx)
                     + _chart(634, 12, 582, 286, p["synth_track"], metric, True, pfx) + "</g>")

    # 下半部：一体化四维胶囊轨道哑铃卡 (x=24..1216, y=312..558)
    parts.append(f'<rect class="panel-card" x="24" y="312" width="1192" height="246" rx="18" filter="url(#{pfx}-shadow)"/>')
    parts.append(_t(48, 344, "星澜市终局四维成绩单（T3 · 两名受试均值）", "card-h-sm"))
    # 右上内联图例
    for k, arm in enumerate(ARMS):
        lx = 770 + k * 146
        parts.append(f'<line class="ln ln-{arm}" x1="{lx}" y1="339" x2="{lx + 24}" y2="339"/>'
                     + _t(lx + 32, 344, _ARM_NAMES[arm], "lbl-xs"))

    x0, x1 = 278, 746
    for k, row in enumerate(p["final_rows"]):
        y = 382 + k * 36

        def X(v: float, row=row) -> float:
            return round(x0 + (x1 - x0) * v / row["max"], 1)

        parts.append(_t(48, y + 5, f"0{k + 1}", "mono-xs")
                     + _t(74, y + 5, row["label"], "lbl-b"))
        # 凹槽胶囊底轨（刻度右端在 x=788 结束，与右侧 x=832 注释区留足 44px 安全间距）
        parts.append(f'<rect class="db-track" x="{x0}" y="{y - 3}" width="{x1 - x0}" height="6" rx="3"/>'
                     + _t(x0 - 12, y + 4, "0", "mono-xs", "end")
                     + _t(x1 + 12, y + 4, f"{row['max']:g}{row['unit'] if row['unit'] == '%' else ''}", "mono-xs"))

        vals = row["values"]
        xi0, xi5, xi4 = X(vals["I0"]), X(vals["I5"]), X(vals["I4"])
        parts.append(f'<line class="db-span" x1="{_n(min(xi5, xi4))}" y1="{y}" x2="{_n(max(xi5, xi4))}" y2="{y}"/>')
        parts.append(f'<rect class="dot-I0" x="{_n(xi0 - 4.5)}" y="{y - 4.5}" width="9" height="9" rx="2.5" '
                     f'data-tip="{_e("I0 · " + row["label"] + " " + _fmt(vals["I0"]))}"/>')
        parts.append(f'<circle class="ring-I5" cx="{_n(xi5)}" cy="{y}" r="7.5" '
                     f'data-tip="{_e("I5 · " + row["label"] + " " + _fmt(vals["I5"]))}"/>')
        parts.append(f'<circle class="dot-I4" cx="{_n(xi4)}" cy="{y}" r="4.5" '
                     f'data-tip="{_e("I4 · " + row["label"] + " " + _fmt(vals["I4"]))}"/>')

        # 智能避让数值标注：两点间距较近时左右反向锚定，彻底杜绝数字挤碰
        if vals["I4"] == vals["I5"]:
            parts.append(_t(xi4, y - 12, f"I5 = I4 · {_fmt(vals['I4'])}", "lbl-xxs t-I4", "middle"))
        elif abs(xi4 - xi5) < 76:
            if xi5 < xi4:
                parts.append(_t(xi5 - 6, y - 11, f"I5 {_fmt(vals['I5'])}", "lbl-xxs t-I5", "end"))
                parts.append(_t(xi4 + 6, y - 11, f"I4 {_fmt(vals['I4'])}", "lbl-xxs t-I4", "start"))
            else:
                parts.append(_t(xi4 - 6, y - 11, f"I4 {_fmt(vals['I4'])}", "lbl-xxs t-I4", "end"))
                parts.append(_t(xi5 + 6, y - 11, f"I5 {_fmt(vals['I5'])}", "lbl-xxs t-I5", "start"))
        else:
            parts.append(_t(xi5, y - 11, f"I5 {_fmt(vals['I5'])}", "lbl-xxs t-I5", "middle"))
            parts.append(_t(xi4, y - 11, f"I4 {_fmt(vals['I4'])}", "lbl-xxs t-I4", "middle"))
        parts.append(_t(xi0, y + 19, f"I0 {_fmt(vals['I0'])}", "lbl-xxs", "middle"))

        # 右侧说明标签列 (x=832..1192)
        if row["lower_better"]:
            parts.append(f'<rect class="flag-box" x="832" y="{y - 14}" width="360" height="26" rx="8"/>'
                         + _t(846, y + 4, row["note"], "lbl-xs t-red"))
        else:
            parts.append(_t(846, y + 4, row["note"], "lbl-xs"))

    # 卡片底栏：防伪账本印记 + 追问锦囊/审计后台轻量胶囊入口
    a = p["audit"]
    parts.append('<line class="hairline" x1="48" y1="516" x2="1192" y2="516"/>'
                 + _t(48, 541, f"防伪账本 · 已结算 {a['settled_calls']} 次调用 · 实付 ¥{float(a['total_charged_cny']):.4f}"
                               f"（授权上限 ¥{a['budget_limit_cny']}） · SHA-256 {a['ledger_sha256'][:12]}…", "mono-xs"))
    parts.append(_pill_btn(852, 521, 156, "老师追问 Q1–Q5", 'data-open="qa"', pfx))
    parts.append('<a href="./dashboard.html" class="sbtn clickable">'
                 f'<rect class="sbtn-bg sbtn-ghost" x="1020" y="521" width="172" height="32" rx="16" '
                 f'filter="url(#{pfx}-pill-sh)"/>'
                 + _t(1106, 541.5, "审计看板 dashboard.html ↗", "sbtn-ghost-t", "middle") + "</a>")
    parts.append("</svg>")
    return "".join(parts)


# ============================================================ 抽屉（静态预渲染，默认关闭）
def _mark(text: str, highlights: list[str]) -> str:
    out = _e(text)
    for h in sorted(set(highlights), key=len, reverse=True):
        out = out.replace(_e(h), f"<mark>{_e(h)}</mark>")
    return out


def _pre(text: str, cls="pre") -> str:
    return f'<div class="{cls}">{_e(text)}</div>'


def _drawer_source(p: dict) -> str:
    out = []
    c, scale = p["clone"], p["scale"]
    rows = "".join(f"<tr><td>{_e(t['topic_id'])}</td><td>{_e(t['label'])}</td><td>{_e(t['subjects'])}</td>"
                   f"<td class=\"r\">{t['calls']}</td></tr>" for t in scale["real_topics"])
    pair_rows = "".join(f"<tr><td>{x['subject']}</td><td>第 {x['round']} 轮</td><td class=\"r\">{x['i5_chars']}</td>"
                        f"<td class=\"r\">{x['i4_chars']}</td><td class=\"r\">{100 * x['total_difference_ratio']:.2f}%</td>"
                        f"<td>7/7 通过</td></tr>" for x in c["pairs"])
    out.append('<div data-only-scene="0"><h3>第 1 幕 · 规模与公平性明细</h3>'
               f'<p>最终运行共 {scale["final_run_calls"]} 次模型调用：现实三议题 {scale["real_final_run_calls"]} 次'
               f'（其中采纳 {scale["real_accepted_calls"]} 次、{scale["real_branches"]} 个分支），星澜市封闭情境 {scale["closed_calls"]} 次；'
               f'真实答卷与独立双盲评估 {scale["answers_and_blind_evaluations"]} 份；另有 {scale["offline_rehearsal_samples"]} 份离线预演样本；'
               f'归档全量回归 {scale["archived_tests"]} 项（{_e(scale["archived_validation"])}）。</p>'
               '<table><thead><tr><th>议题</th><th>名称</th><th>受试</th><th class="r">调用</th></tr></thead>'
               f'<tbody>{rows}<tr><td>TOPIC_SYNTH_01</td><td>星澜市（§5.4 合成）</td><td>S01 / S02</td>'
               f'<td class="r">{scale["closed_calls"]}</td></tr></tbody></table>'
               '<h4>I4 / I5 逐轮配长（可见全文字数）</h4><table><thead><tr><th>受试</th><th>轮次</th><th class="r">I5</th>'
               f'<th class="r">I4</th><th class="r">总长差</th><th>七项检查</th></tr></thead><tbody>{pair_rows}</tbody></table>'
               f'<p class="muted">七项检查：{"、".join(PAIR_CHECKS)}。门禁：可见字数 600–800、长度差 ≤10%。</p></div>')
    ev = {e["evidence_id"]: e for e in p["evidence"]}
    for s in p["subjects"]:
        sid = s["subject_id"]
        mirror = "".join(
            f'<li id="mirror-{sid}-{k}"><span class="tag">{_e(it["section"])}</span>{_e(it["text"])}'
            + (f'<div class="quote">引句：{_e(it["user_quote"])}</div>' if it.get("user_quote") else "")
            + (f'<div class="muted">证据：{_e("、".join(it["citations"]))}</div>' if it["citations"] else "") + "</li>"
            for k, it in enumerate(s["mirror"]["items"]))
        feed = "".join(
            f'<tr class="{"rumor-row" if card["misleading"] else ""}"><td class="mono">R{card["round"]}-{card["slot"]}</td>'
            f'<td>{_e({"same": "同阵营", "neutral": "中立", "opposite": "对立"}[card["relation"]])}'
            f'{"<br><b class=red>含失真数字</b>" if card["misleading"] else ""}</td><td>{_e(card["text"])}'
            + "".join(f'<div class="factnote">核查旁注（曝光时受试不可见）{_e(eid)}：{_e(ev[eid]["text"])}</div>'
                      for eid in card["evidence_ids"]) + "</td></tr>"
            for card in s["feed"])
        out.append(f'<div data-only-scene="1" data-only-subject="{sid}"><h3>第 2 幕 · {sid} 发帖原文与认知镜</h3>'
                   f'<p class="muted">初始倾向：{_e(s["side_text"])}。高亮为转述的网传数字（逐字原文）。</p>'
                   f'<blockquote>{_mark(s["speech"], s["speech_highlights"])}</blockquote>'
                   f'<p>T1 测量：{s["t1_items_at_half"]}/{s["t1_items"]} 题 p_true = 0.50。立场刻度 {s["mirror"]["stance_pct"]}%。</p>'
                   f'<h4>认知镜 mirror 全文</h4><ol class="mirror">{mirror}</ol>'
                   f'<h4>8 轮 48 张曝光帖</h4><table class="feed"><thead><tr><th>轮-位</th><th>类型</th><th>原帖</th></tr></thead>'
                   f'<tbody>{feed}</tbody></table></div>')
        for r_key, rc in s["rounds"].items():
            flag = ""
            if rc["review_flag"]:
                f = rc["review_flag"]
                flag = (f'<div class="review-flag"><b>待真人复核：证据编号错位</b><div>{_e(f["sentence"])}</div>'
                        f'<div>{_e(f["note"])}</div></div>')
            checks = "、".join(f"{k}={'通过' if v else '未通过'}" for k, v in rc["pair_checks"].items())
            cols = "".join(
                f'<div class="col col-{arm}"><h4>{_e(_ARM_NAMES[arm])} · {chars} 字</h4>{_pre(body)}'
                f'<h5>受试回应</h5>{_pre(rc["responses"][arm], "pre resp")}</div>'
                for arm, chars, body in (("I0", rc["i0_chars"], rc["i0_visible_text"]),
                                         ("I5", rc["i5_chars"], "阅读说明（确定性原句摘录）\n" + rc["i5_addition"]),
                                         ("I4", rc["i4_chars"], "阅读说明（破茧助手组织段）\n" + rc["i4_addition"])))
            out.append(f'<div data-only-scene="2" data-only-subject="{sid}" data-only-round="{r_key}">'
                       f'<h3>第 3 幕 · {sid} 第 {r_key} 轮（{_e(rc["theme"])}）三轨完整原文</h3>{flag}'
                       f'<h4>共享核查证据块（I5 与 I4 逐字相同：{_e("、".join(rc["shared_evidence_ids"]))}）</h4>'
                       f'{_pre(rc["shared_evidence_block"])}<div class="cols">{cols}</div>'
                       f'<p class="muted">七项配对检查：{_e(checks)}；总长差 {100 * rc["total_difference_ratio"]:.2f}%，'
                       f'阅读说明长度差 {100 * rc["additional_difference_ratio"]:.2f}%。I5/I4 可见全文 = 共享证据块 + 阅读说明 + 统一回应提示。</p>'
                       + ("<p class=\"muted\">I0 两轮回应逐字相同（已在边界中披露）。</p>" if s["i0_responses_identical"] else "")
                       + "</div>")
    sb = "".join(
        f"<tr><td>{_e(r['name'])}</td>" + "".join(f"<td class=\"r\">{r['acc_' + t]:g}</td>" for t in ("t0", "t1", "t2", "t3"))
        + "".join(f"<td class=\"r\">{r['bs_' + t]:.4f}</td>" for t in ("t0", "t1", "t2", "t3"))
        + f"<td class=\"r\">{r['transfer_correct_t3']:g}/{r['transfer_total']}</td><td class=\"r\">{r['u_t3']:g}</td>"
          f"<td class=\"r\">{r['o_t3']:g}</td></tr>" for r in p["scoreboard"])
    per = "".join(
        f"<tr><td>{s['subject_id']}</td><td>{arm}</td><td class=\"r\">{s['t3'][arm]['Acc']:g}</td>"
        f"<td class=\"r\">{s['t3'][arm]['BS']:.4f}</td><td class=\"r\">{s['t3'][arm]['transfer_correct']}/4</td>"
        f"<td class=\"r\">{s['t3'][arm]['U']:g}</td><td class=\"r\">{s['t3'][arm]['O']:g}</td></tr>"
        for s in p["subjects"] for arm in ARMS)
    real = "".join(f"<tr><td>{r['topic_id']}</td><td>{r['arm']}</td><td class=\"r\">{r['acc_t0']:g}</td>"
                   f"<td class=\"r\">{r['acc_t3']:g}</td><td class=\"r\">{r['bs_t0']:.4f}</td><td class=\"r\">{r['bs_t3']:.4f}</td></tr>"
                   for r in p["real_track"]["by_topic"])
    out.append('<div data-only-scene="3"><h3>第 4 幕 · 终局数值明细</h3><h4>星澜市三臂均值（2 名受试）</h4>'
               '<table><thead><tr><th>臂</th><th>Acc T0</th><th>T1</th><th>T2</th><th>T3</th><th>BS T0</th><th>T1</th>'
               '<th>T2</th><th>T3</th><th>迁移</th><th>U</th><th>O</th></tr></thead>'
               f'<tbody>{sb}</tbody></table><h4>逐受试 T3</h4><table><thead><tr><th>受试</th><th>臂</th><th>Acc</th>'
               f'<th>BS</th><th>迁移</th><th>U</th><th>O</th></tr></thead><tbody>{per}</tbody></table>'
               '<h4>现实三议题（天花板）</h4><table><thead><tr><th>议题</th><th>臂</th><th>Acc T0</th><th>Acc T3</th>'
               f'<th>BS T0</th><th>BS T3</th></tr></thead><tbody>{real}</tbody></table>'
               '<p class="muted">O 为盲区遗漏率，越低越好；主终点为 T3 的 Brier 分数（BS）。</p></div>')
    return "".join(out)


def _drawer(p: dict) -> str:
    evidence = "".join(f'<article class="ev"><h4>{_e(e["evidence_id"])} · 第 {e["round"]} 轮 · {_e(e["publisher"])} '
                       f'{_e(e["date"])}</h4><p>{_e(e["text"])}</p></article>' for e in p["evidence"])
    qa = "".join(f'<article class="qa"><h4>{_e(q["q"])}</h4><p>{_e(q["a"])}</p></article>' for q in p["qa_cards"])
    a = p["audit"]
    boundary = ("<ol class=\"boundary\">" + "".join(f"<li>{_e(x)}</li>" for x in p["boundary"]) + "</ol>"
                '<h4>账本与指纹（防伪）</h4><table><tbody>'
                f'<tr><td>已结算调用</td><td class="mono">{a["settled_calls"]}</td></tr>'
                f'<tr><td>累计结算</td><td class="mono">¥{_e(a["total_charged_cny"])}（上限 ¥{_e(a["budget_limit_cny"])}）</td></tr>'
                f'<tr><td>持有未结算预留</td><td class="mono">¥{_e(a["held_unsettled_reserved_cny"])}</td></tr>'
                f'<tr><td>账本 SHA-256</td><td class="mono">{_e(a["ledger_sha256"])}</td></tr>'
                f'<tr><td>看板数据集 SHA-256</td><td class="mono">{_e(p["source_dataset_sha256"])}</td></tr>'
                f'<tr><td>封闭情境清单 SHA-256</td><td class="mono">{_e(p["closed_manifest_sha256"])}</td></tr>'
                f'<tr><td>三议题矩阵 SHA-256</td><td class="mono">{_e(p["matrix_sha256"])}</td></tr>'
                f'<tr><td>封闭情境运行</td><td class="mono">{_e(p["closed_run_id"])}</td></tr>'
                f'<tr><td>归档全量回归</td><td class="mono">{p["scale"]["archived_tests"]} 项 · {_e(p["scale"]["archived_validation"])}</td></tr>'
                '</tbody></table>')
    return ('<div id="scrim" class="scrim" hidden></div>'
            '<aside id="drawer" class="drawer" aria-hidden="true" aria-label="原文与核验抽屉" data-tab="source">'
            '<div class="dr-head"><div class="dr-tabs" role="tablist">'
            '<button type="button" class="dr-tab" data-tab="source">当前幕原文对照</button>'
            '<button type="button" class="dr-tab" data-tab="evidence">共享核查证据</button>'
            '<button type="button" class="dr-tab" data-tab="qa">老师追问 Q1–Q5</button>'
            '<button type="button" class="dr-tab" data-tab="boundary">科研诚实边界与账本防伪</button></div>'
            '<button type="button" class="btn" id="dr-close">关闭 Esc</button></div>'
            '<div class="dr-body" id="dr-body">'
            f'<section class="pane" data-pane="source">{_drawer_source(p)}</section>'
            f'<section class="pane" data-pane="evidence"><h3>冻结核查证据（6 条，逐字）</h3>{evidence}</section>'
            f'<section class="pane" data-pane="qa"><h3>老师追问锦囊</h3>{qa}</section>'
            f'<section class="pane" data-pane="boundary"><h3>科研诚实边界与实验家底</h3>{boundary}</section>'
            '</div></aside>')


# ============================================================ 高定科学编辑部样式表
_CSS = """
:root{
  --canvas:#F8F6F1;--surface:#FFFFFF;--surface-2:#F1EFE8;--surface-glass:rgba(248,246,241,.86);
  --ink:#16181D;--ink-2:#5A5F6B;--ink-3:#8B909E;--border:rgba(22,24,29,.085);--border-strong:rgba(22,24,29,.16);
  --rule:#E6E2D8;--dot:rgba(22,24,29,.055);
  --elev:0 1px 2px rgba(15,23,42,.04),0 14px 34px -10px rgba(15,23,42,.07);
  --red:#D6453A;--teal:#12787F;--amber:#B86B11;--green:#1C7A47;--slate:#8792A2;
  --red-soft:rgba(214,69,58,.085);--teal-soft:rgba(18,120,127,.095);
  --amber-soft:rgba(184,107,17,.10);--green-soft:rgba(28,122,71,.09);--ink-soft:rgba(22,24,29,.045)
}
[data-theme="dark"]{
  --canvas:#0E1015;--surface:#161922;--surface-2:#1E222E;--surface-glass:rgba(14,16,21,.86);
  --ink:#EDEBE6;--ink-2:#A2A7B4;--ink-3:#6F7685;--border:rgba(255,255,255,.09);--border-strong:rgba(255,255,255,.18);
  --rule:#282D3B;--dot:rgba(255,255,255,.05);
  --elev:0 2px 6px rgba(0,0,0,.35),0 18px 40px -10px rgba(0,0,0,.55);
  --red:#E86A60;--teal:#3AB0B8;--amber:#E29A3B;--green:#49B075;--slate:#7E899B;
  --red-soft:rgba(232,106,96,.13);--teal-soft:rgba(58,176,184,.13);
  --amber-soft:rgba(226,154,59,.14);--green-soft:rgba(73,176,117,.13);--ink-soft:rgba(255,255,255,.06)
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{
  background-color:var(--canvas);
  background-image:radial-gradient(circle,var(--dot) 1px,transparent 1.2px);
  background-size:28px 28px;color:var(--ink);
  font-family:"PingFang SC","Microsoft YaHei UI","Noto Sans CJK SC","Source Han Sans SC",system-ui,sans-serif;
  font-size:15px;line-height:1.58;-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums
}
.mono,.timer{font-family:ui-monospace,"SFMono-Regular","Cascadia Mono",Consolas,monospace;font-variant-numeric:tabular-nums}
button{font:inherit}
.btn{
  font-size:12.5px;font-weight:500;line-height:1;border:1px solid var(--border);background:var(--surface);
  color:var(--ink-2);padding:7px 12px;border-radius:999px;cursor:pointer;text-decoration:none;
  white-space:nowrap;display:inline-flex;align-items:center;gap:5px;
  transition:all .2s cubic-bezier(.22,1,.36,1)
}
.btn:hover{color:var(--ink);border-color:var(--border-strong);transform:translateY(-1px)}
.btn.on{background:var(--ink);color:var(--surface);border-color:var(--ink);font-weight:600}
.btn-accent{background:var(--ink);color:var(--surface);border-color:var(--ink);font-weight:600}
.btn-accent:hover{background:var(--amber);border-color:var(--amber);color:#fff}
.seg-pill{display:inline-flex;background:var(--surface-2);padding:3px;border-radius:999px;border:1px solid var(--border);gap:2px}
.seg-pill .btn{border:0;background:transparent;padding:5px 11px;transform:none}
.seg-pill .btn.on{background:var(--surface);color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.08)}
.btn:focus-visible,.clickable:focus-visible,[data-card]:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
header.bar{
  height:52px;display:flex;align-items:center;gap:16px;padding:0 24px;
  border-bottom:1px solid var(--border);background:var(--surface-glass);
  backdrop-filter:blur(14px);position:relative;z-index:20
}
.brand{display:flex;align-items:center;gap:9px;white-space:nowrap}
.brand b{font-size:15px;font-weight:700;letter-spacing:.3px}
.brand span{color:var(--ink-3);font-size:12.5px;padding-left:8px;border-left:1px solid var(--rule)}
.logo{width:22px;height:22px}
.logo-ring{fill:none;stroke:var(--ink);stroke-width:1.4;opacity:.85}
.logo-lens{fill:var(--amber-soft);stroke:var(--amber);stroke-width:1.4}
.prog{
  display:inline-flex;align-items:center;background:var(--surface-2);padding:3px;
  border-radius:999px;border:1px solid var(--border);gap:2px;margin:0 auto
}
.node{
  border:0;background:transparent;color:var(--ink-2);font-size:12.5px;font-weight:500;
  height:28px;padding:0 12px;border-radius:999px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;
  transition:all .22s cubic-bezier(.22,1,.36,1)
}
.node .nc{font-family:ui-monospace,Consolas,monospace;font-size:11px;opacity:.65}
.node:hover{color:var(--ink)}
.node.on{background:var(--surface);color:var(--ink);font-weight:600;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.node.on .nc{color:var(--amber);opacity:1}
.node.done .nc{color:var(--teal);opacity:.95}
.seg{display:none}
.tools{display:flex;align-items:center;gap:6px}
.timer-wrap{display:inline-flex;align-items:center;gap:6px;background:var(--surface-2);padding:3px 4px 3px 10px;border-radius:999px;border:1px solid var(--border)}
.timer{font-size:13.5px;font-weight:600;min-width:44px;text-align:center;color:var(--ink)}
.timer.over{color:var(--red)}
.timer-wrap .btn{padding:5px 10px;font-size:12px}
.pace{position:absolute;left:0;right:0;bottom:-1px;height:2px;overflow:hidden}
.pace i{display:block;height:100%;width:0;background:var(--amber);transition:width 1s linear}
main.stage{position:relative;max-width:1520px;margin:0 auto}
.scene{display:none;padding:12px 24px 24px;grid-template-rows:auto minmax(0,1fr);gap:8px}
.scene.active{display:grid;animation:sceneIn .32s cubic-bezier(.22,1,.36,1)}
@keyframes sceneIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.scene-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;padding:0 4px}
.eyebrow-row{display:flex;align-items:center;gap:8px;margin-bottom:2px}
.scene-tag{font-family:ui-monospace,Consolas,monospace;font-size:11px;font-weight:600;letter-spacing:.08em;color:var(--amber);text-transform:uppercase}
.win{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--ink-3)}
.scene-head h2{
  margin:0;font-family:"Songti SC","Noto Serif CJK SC","Source Han Serif SC","STSong",Georgia,serif;
  font-size:28px;line-height:1.22;font-weight:700;letter-spacing:-.01em;color:var(--ink)
}
.lede{margin:3px 0 0;color:var(--ink-2);font-size:14px;white-space:nowrap}
.ctrls{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end;align-items:center}
.ctrls .ind{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--ink-2);min-width:60px;text-align:center}
.stage-box{position:relative;min-height:0;background:transparent;border:0}
svg.sandbox{display:block;width:100%;height:100%}
@media (min-width:1024px){body{overflow:hidden}main.stage{height:calc(100vh - 52px)}.scene{height:100%}}
@media (max-width:1023px){header.bar{overflow-x:auto}.scene-head{flex-direction:column;align-items:flex-start}
.lede{white-space:normal}.stage-box{height:auto}svg.sandbox{height:auto;min-height:58vh}}
.honest-corner{
  position:fixed;left:28px;bottom:6px;z-index:15;border:0;background:transparent;color:var(--ink-3);
  font-size:11px;cursor:pointer;padding:2px 0;transition:color .18s
}
.honest-corner:hover{color:var(--ink);text-decoration:underline}

/* SVG 高定图元系统 */
svg text{fill:var(--ink);font-family:"PingFang SC","Microsoft YaHei UI","Noto Sans CJK SC",sans-serif}
.card-h{
  font-family:"Songti SC","Noto Serif CJK SC","Source Han Serif SC","STSong",Georgia,serif;
  font-size:17.5px;font-weight:700;letter-spacing:.01em
}
.card-h-sm{font-size:15px;font-weight:700;letter-spacing:.01em}
.kpi-num{font-family:ui-monospace,Consolas,monospace;font-size:23px;font-weight:700;letter-spacing:-.02em}
.lbl{font-size:14px}.lbl-s{font-size:13px;fill:var(--ink-2)}.lbl-xs{font-size:12px;fill:var(--ink-2)}
.lbl-xxs{font-size:11.2px;fill:var(--ink-3)}
.lbl-b{font-size:14px;font-weight:600}
.mono-b,.mono-xs,.num-b,.num-xs{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-variant-numeric:tabular-nums}
.mono-b{font-size:13px;font-weight:600}.mono-xs{font-size:11.2px;fill:var(--ink-2)}
.num-b{font-size:15px;font-weight:700}.num-xs{font-size:11px;font-weight:700}
svg .t-red{fill:var(--red)}svg .t-I0{fill:var(--slate)}svg .t-I5{fill:var(--teal)}
svg .t-I4{fill:var(--amber)}svg .t-green{fill:var(--green)}
.panel-card{fill:var(--surface);stroke:var(--border);stroke-width:1;transition:stroke .22s,transform .22s}
.inset-box{fill:var(--surface-2);stroke:var(--border);stroke-width:1}
.hairline{stroke:var(--rule);stroke-width:1}
.grid{stroke:var(--rule);stroke-width:1;stroke-dasharray:2 3}
.chip-bg{fill:var(--surface-2);stroke:var(--border);stroke-width:1}
.hash-pill{fill:var(--surface-2);stroke:var(--border);stroke-width:1}
.ok-pill{fill:var(--green-soft);stroke:rgba(28,122,71,.25);stroke-width:1}
svg .ok-pill-t{fill:var(--green);font-size:11px;font-weight:600}

/* 双环认知核徽章 */
.ag-orbit{fill:none;stroke:var(--ink-3);stroke-width:1;stroke-dasharray:3 4;opacity:.45}
.ag-disc{fill:var(--surface)}
.ag-rim{fill:none;stroke:var(--border-strong);stroke-width:1.4}
.ag-head{fill:var(--ink);opacity:.86}
.ag-bust{fill:var(--ink);opacity:.78}
.ag-teal .ag-rim{stroke:var(--teal)}.ag-teal .ag-head,.ag-teal .ag-bust{fill:var(--teal)}
.ag-amber .ag-rim{stroke:var(--amber)}.ag-amber .ag-head,.ag-amber .ag-bust{fill:var(--amber)}
.ag-red .ag-rim{stroke:var(--red);stroke-opacity:.65}
.ag-muted .ag-head,.ag-muted .ag-bust{fill:var(--slate);opacity:.72}
.av-tag-bg{stroke:var(--surface);stroke-width:1.5}
.av-tag-muted{fill:var(--slate)}.av-tag-teal{fill:var(--teal)}.av-tag-amber{fill:var(--amber)}
svg .av-tag-t{fill:#fff;font-size:11px;font-weight:700}
.av-check{fill:none;stroke:#fff;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}

/* 双弧光学认知镜透镜 */
.lens-body{stroke:var(--amber);stroke-width:1.4;stroke-opacity:.75}
.lens-spec{fill:none;stroke:#FFFFFF;stroke-width:1.6;stroke-linecap:round;opacity:.8}
.lens-axis{stroke:var(--amber);stroke-width:1;stroke-dasharray:2 3;opacity:.45}
.lens-cap{fill:var(--ink);opacity:.75}

/* 第 1 幕：培养皿与天平 */
.dish-outer{fill:var(--surface-2);fill-opacity:.55;stroke:var(--border-strong);stroke-width:1.2}
.dish-mid{fill:none;stroke:var(--rule);stroke-width:1;stroke-dasharray:4 4}
.dish-inner{fill:var(--surface);fill-opacity:.7;stroke:var(--rule);stroke-width:1}
.pt-same{fill:var(--ink);opacity:.82}
.pt-rumor{fill:var(--red-soft);stroke:var(--red);stroke-width:2}
.pt-both{fill:var(--ink);stroke:var(--red);stroke-width:2.4}
.pt-neither{fill:var(--surface);stroke:var(--slate);stroke-width:1.5}
.orbit-cw{transform-origin:208px 218px;animation:spinCw 54s linear infinite}
.orbit-ccw{transform-origin:208px 218px;animation:spinCcw 40s linear infinite}
@keyframes spinCw{to{transform:rotate(360deg)}}
@keyframes spinCcw{to{transform:rotate(-360deg)}}
.venn-a{fill:var(--ink-soft);stroke:var(--ink);stroke-width:1.2;stroke-opacity:.45}
.venn-b{fill:var(--red-soft);stroke:var(--red);stroke-width:1.3;stroke-opacity:.75}
.conduit-in{stroke:var(--ink-3);stroke-width:1.5;stroke-dasharray:4 4;fill:none}
.gate-outer{fill:none;stroke:var(--amber);stroke-width:1.2;stroke-dasharray:4 4;opacity:.65}
.gate-core{fill:var(--surface);stroke:var(--border-strong);stroke-width:1.5}
.branch{fill:none;stroke-width:2.2;stroke-linecap:round;opacity:.82}
.b-I0{stroke:var(--slate)}.b-I5{stroke:var(--teal)}.b-I4{stroke:var(--amber)}
.acc-I0{fill:var(--slate)}.acc-I5{fill:var(--teal)}.acc-I4{fill:var(--amber)}
.arm-chip{stroke-width:1}
.chip-I0{fill:var(--ink-soft);stroke:rgba(135,146,162,.35)}
.chip-I5{fill:var(--teal-soft);stroke:rgba(18,120,127,.3)}
.chip-I4{fill:var(--amber-soft);stroke:rgba(184,107,17,.35)}
.arm-chip-t{font-family:ui-monospace,Consolas,monospace;font-size:13px;font-weight:700}
.step-pill{fill:var(--amber-soft);stroke:rgba(184,107,17,.32);stroke-width:1}
.step-link{stroke:var(--amber);stroke-width:1.2;opacity:.55}
.token{stroke:var(--surface);stroke-width:2}
.tk-I0{fill:var(--slate)}.tk-I5{fill:var(--teal)}.tk-I4{fill:var(--amber)}
#clone-stage.run .ch-I4{stroke:rgba(184,107,17,.45)}
.bal-base,.bal-pillar{stroke:var(--ink-2);stroke-width:2;stroke-linecap:round}
.bal-dial{fill:var(--surface);stroke:var(--amber);stroke-width:2}
.bal-beam{stroke:var(--ink);stroke-width:2.2;stroke-linecap:round}
.bal-cord{stroke:var(--ink-3);stroke-width:1;fill:none}
.bal-pan{fill:none;stroke-width:2;stroke-linecap:round}
.pan-i5{stroke:var(--teal)}.pan-i4{stroke:var(--amber)}

/* 画布胶囊按钮与悬停微交互 */
.clickable{cursor:pointer}
.clickable:hover .panel-card,.clickable:hover .gate-core,.diag-row:hover .diag-card{stroke:var(--amber);stroke-opacity:.65}
.sbtn-bg{fill:var(--ink);transition:fill .2s}
svg .sbtn-t{fill:var(--surface);font-size:12.5px;font-weight:600}
.sbtn:hover .sbtn-bg{fill:var(--amber)}
.sbtn-ghost{fill:var(--surface-2);stroke:var(--border-strong);stroke-width:1}
svg .sbtn-ghost-t{fill:var(--ink);font-size:12.5px;font-weight:600}
.sbtn:hover .sbtn-ghost{fill:var(--ink)}
.sbtn:hover .sbtn-ghost-t{fill:var(--surface)}

/* 第 2 幕：曝光晶格、原话气泡与精密表盘 */
.bc-same{fill:var(--ink);opacity:.84}
.bc-neutral{fill:var(--surface-2);stroke:var(--ink-3);stroke-width:1.3}
.bc-opp{fill:var(--surface);stroke:var(--slate);stroke-width:1.6}
.rumor-ring{fill:var(--red-soft);stroke:var(--red);stroke-width:1.4;stroke-opacity:.7}
.rumor-dot{fill:var(--red)}
.beacon{cursor:help;transition:opacity .28s ease}
.beacon:hover{opacity:.85}
.beacon.rumor .rumor-ring{animation:pulseHalo 2.8s ease-in-out infinite}
@keyframes pulseHalo{0%,100%{stroke-opacity:.75;fill-opacity:.12}50%{stroke-opacity:.28;fill-opacity:.04}}
.pulse{fill:none;stroke:var(--red);stroke-width:1.2;stroke-dasharray:3 5;opacity:.14}
.dye.playing .beacon{opacity:.15}
.dye.playing .pulse{opacity:0}
.dye.playing .beacon.lit{opacity:1}
.dye.playing .pulse.lit{opacity:.58;animation:streamIn .85s cubic-bezier(.22,1,.36,1)}
@keyframes streamIn{from{stroke-dashoffset:140;opacity:.95}to{stroke-dashoffset:0;opacity:.55}}
.zone-exp{fill:var(--red-soft);stroke:rgba(214,69,58,.16);stroke-width:1}
.zone-meas{fill:var(--surface-2);stroke:var(--border);stroke-width:1}
.bubble{transition:opacity .45s ease}
.dye.playing .bubble{opacity:.15}
.dye.played .bubble{opacity:1}
.bubble-card{fill:var(--surface);stroke:var(--border-strong);stroke-width:1}
.b-tone-red .bubble-bar{fill:var(--red)}
svg .bubble-t{fill:var(--ink);font-size:13px;font-weight:600}
.stem-red{stroke:var(--red);stroke-width:1.3;stroke-dasharray:3 3;opacity:.55}
.stem-teal{stroke:var(--teal);stroke-width:1.3;stroke-dasharray:3 3;opacity:.55}
.gauge-safe{fill:var(--teal-soft)}
.gauge-track{fill:none;stroke:var(--ink);stroke-width:5;stroke-linecap:round;opacity:.82}
.gauge-tick{stroke:var(--ink-3);stroke-width:1.3;stroke-linecap:round}
.needle{stroke:var(--teal);stroke-width:3.2;stroke-linecap:round}
.needle-hub{fill:var(--ink)}.needle-core{fill:var(--surface)}
.scan-beam{stroke:var(--amber);stroke-width:1.5;stroke-dasharray:4 4;opacity:.65}
.spec-tick{stroke:var(--surface);stroke-width:1.5}
.spec-pill{fill:var(--amber-soft);stroke:rgba(184,107,17,.35);stroke-width:1}
.spec-ptr{fill:var(--amber)}
.fiber{fill:none;stroke:var(--amber);stroke-width:1.2;opacity:.48}
.fiber-teal{stroke:var(--teal)}
.diag-card{fill:var(--surface-2);stroke:var(--border);stroke-width:1;transition:all .2s}
.dot-bias{fill:var(--red)}.dot-blind{fill:var(--teal)}

/* 第 3 幕：平行泳道与跨轨共享证据桥 */
.wave{fill:none;stroke:var(--slate);stroke-width:1.8;stroke-dasharray:4 4}
.flow-muted{stroke:var(--slate);stroke-width:1.5;stroke-dasharray:3 4}
.ev-box{fill:var(--green-soft);stroke:rgba(28,122,71,.32);stroke-width:1.2}
.ex-box{fill:var(--teal-soft);stroke:rgba(18,120,127,.32);stroke-width:1.2}
.sig{stroke:var(--amber);stroke-width:1.9;fill:none;stroke-linecap:round}
.sig-I5{stroke:var(--teal)}
.q-tail{fill:var(--surface);stroke:var(--border-strong);stroke-width:1}
.bar-I0{fill:var(--slate)}.bar-I5{fill:var(--teal)}.bar-I4{fill:var(--amber)}
svg .quote-t{fill:var(--ink);font-size:12.6px;font-weight:500}
.bridge-line{stroke:var(--green);stroke-width:1.8;stroke-dasharray:4 3}
.bridge-pill{fill:var(--surface);stroke:rgba(28,122,71,.42);stroke-width:1.2}
svg .bridge-t{fill:var(--green);font-size:11px;font-weight:700}
.flag-pill{fill:var(--red-soft);stroke:rgba(214,69,58,.38);stroke-width:1}
svg .flag-pill-t{fill:var(--red);font-size:10.8px;font-weight:600}
.ring-bg{fill:none;stroke:var(--rule);stroke-width:4.5}
.ring-val{fill:none;stroke-width:4.5;stroke-linecap:round}
.rv-I0{stroke:var(--slate)}.rv-I5{stroke:var(--teal)}.rv-I4{stroke:var(--amber)}
.cell-off{fill:var(--surface-2);stroke:var(--rule);stroke-width:1.2}
.cell-on{stroke:none}.c-I0{fill:var(--slate)}.c-I5{fill:var(--teal)}.c-I4{fill:var(--amber)}

/* 第 4 幕：双轨曲线与胶囊哑铃成绩单 */
.reflux{fill:var(--red-soft)}
.guess{stroke:var(--ink-3);stroke-width:1.2;stroke-dasharray:5 4}
.ceiling-band{fill:var(--amber-soft);stroke:rgba(184,107,17,.22);stroke-width:1}
.ln{fill:none;stroke-linejoin:round;stroke-linecap:round;stroke-width:2.6}
.ln-I0{stroke:var(--slate);stroke-dasharray:5 4;stroke-width:2.2}
.ln-I5{stroke:var(--teal)}.ln-I4{stroke:var(--amber)}
.draw{stroke-dasharray:1400;stroke-dashoffset:0;animation:drawLine 1.5s cubic-bezier(.22,1,.36,1) both}
@keyframes drawLine{from{stroke-dashoffset:1400}to{stroke-dashoffset:0}}
.pt{stroke:var(--surface);stroke-width:1.6;cursor:help}
.pt-I0{fill:var(--slate)}.pt-I5{fill:var(--teal)}.pt-I4{fill:var(--amber)}
.dual-pt{cursor:help}
.pt-dual-out{fill:var(--surface);stroke:var(--teal);stroke-width:2.4}
.pt-dual-in{fill:var(--amber)}
.db-track{fill:var(--surface-2)}
.db-span{stroke:var(--ink-2);stroke-width:2.4;stroke-linecap:round;opacity:.55}
.dot-I0{fill:var(--slate);cursor:help}
.ring-I5{fill:var(--surface);stroke:var(--teal);stroke-width:2.8;cursor:help}
.dot-I4{fill:var(--amber);cursor:help}
.flag-box{fill:var(--red-soft);stroke:rgba(214,69,58,.32);stroke-width:1}

body[data-subject="S01"] [data-only-subject="S02"],body[data-subject="S02"] [data-only-subject="S01"],
body[data-round="1"] [data-only-round="2"],body[data-round="2"] [data-only-round="1"],
body[data-metric="acc"] [data-only-metric="brier"],body[data-metric="brier"] [data-only-metric="acc"]{display:none}
.pane [data-only-scene]{display:none}
body[data-scene="0"] .pane [data-only-scene="0"],body[data-scene="1"] .pane [data-only-scene="1"],
body[data-scene="2"] .pane [data-only-scene="2"],body[data-scene="3"] .pane [data-only-scene="3"]{display:block}
body[data-subject="S01"] .pane [data-only-subject="S02"],body[data-subject="S02"] .pane [data-only-subject="S01"],
body[data-round="1"] .pane [data-only-round="2"],body[data-round="2"] .pane [data-only-round="1"]{display:none}

/* 侧滑抽屉与毛玻璃浮层 */
.scrim{position:fixed;inset:0;background:rgba(15,18,25,.32);backdrop-filter:blur(3px);z-index:40}
.scrim[hidden]{display:none}
.drawer{
  position:fixed;top:0;right:0;bottom:0;width:min(780px,94vw);z-index:50;background:var(--surface);
  border-left:1px solid var(--border-strong);box-shadow:-16px 0 48px rgba(0,0,0,.14);
  transform:translateX(104%);transition:transform .3s cubic-bezier(.22,1,.36,1);
  display:flex;flex-direction:column;visibility:hidden
}
.drawer.open{transform:none;visibility:visible}
.drawer.wide{width:min(1260px,97vw)}
.dr-head{display:flex;align-items:center;gap:10px;padding:12px 18px;border-bottom:1px solid var(--border);background:var(--canvas)}
.dr-tabs{display:flex;flex-wrap:wrap;gap:6px;flex:1}
.dr-tab{border:1px solid var(--border);background:var(--surface);color:var(--ink-2);font-size:12.5px;padding:6px 12px;border-radius:999px;cursor:pointer}
.drawer[data-tab="source"] .dr-tab[data-tab="source"],.drawer[data-tab="evidence"] .dr-tab[data-tab="evidence"],
.drawer[data-tab="qa"] .dr-tab[data-tab="qa"],.drawer[data-tab="boundary"] .dr-tab[data-tab="boundary"]{
  background:var(--ink);color:var(--surface);border-color:var(--ink);font-weight:600
}
.dr-body{overflow:auto;padding:18px 22px 44px;flex:1}
.pane{display:none}
.drawer[data-tab="source"] .pane[data-pane="source"],.drawer[data-tab="evidence"] .pane[data-pane="evidence"],
.drawer[data-tab="qa"] .pane[data-pane="qa"],.drawer[data-tab="boundary"] .pane[data-pane="boundary"]{display:block}
.dr-body h3{font-family:"Songti SC","Noto Serif CJK SC",Georgia,serif;font-size:20px;margin:4px 0 12px}
.dr-body h4{font-size:14.5px;margin:18px 0 8px}.dr-body h5{font-size:12.5px;margin:12px 0 4px;color:var(--ink-2)}
.dr-body table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}
.dr-body th,.dr-body td{border-bottom:1px solid var(--rule);padding:7px 8px;text-align:left;vertical-align:top}
.dr-body td.r,.dr-body th.r{text-align:right;font-family:ui-monospace,Consolas,monospace}
.dr-body blockquote{margin:8px 0;padding:10px 14px;border-left:3px solid var(--red);background:var(--canvas);border-radius:0 8px 8px 0}
.dr-body mark{background:var(--red-soft);color:var(--red);padding:1px 3px;border-radius:3px}
.pre{white-space:pre-wrap;background:var(--canvas);border:1px solid var(--border);border-radius:10px;padding:10px 12px;font-size:13px;line-height:1.68}
.pre.resp{border-left:3px solid var(--ink-2)}
.cols{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:10px}
.col h4{margin-top:0}.col-I0 h4{color:var(--slate)}.col-I5 h4{color:var(--teal)}.col-I4 h4{color:var(--amber)}
.review-flag{border:1px solid rgba(214,69,58,.4);background:var(--red-soft);border-radius:10px;padding:10px 12px;margin:10px 0;font-size:13px}
.factnote{margin-top:5px;font-size:12px;color:var(--green);border-left:2px solid var(--green);padding-left:8px}
.rumor-row td:first-child{box-shadow:inset 3px 0 0 var(--red)}
.red{color:var(--red)}.muted{color:var(--ink-2);font-size:13px}
.tag{display:inline-block;font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--ink-2);background:var(--surface-2);padding:1px 6px;border-radius:4px;margin-right:6px}
.quote{font-size:12px;color:var(--ink-2);margin-top:3px}
ol.mirror li,ol.boundary li{margin:8px 0}
.ev,.qa{border-bottom:1px solid var(--rule);padding:6px 0 10px}
.hl{background:var(--amber-soft);border-radius:6px;padding:4px 6px}

/* 提词浮窗（默认隐藏）与悬停浮层 */
.tele{
  position:fixed;right:20px;bottom:20px;width:min(360px,92vw);z-index:30;background:var(--surface-glass);
  backdrop-filter:blur(16px);border:1px solid var(--border-strong);border-radius:14px;
  box-shadow:var(--elev);padding:12px 14px;font-size:13px
}
.tele.hidden{display:none}
.tele-head{display:flex;align-items:center;gap:6px;margin-bottom:8px}
.tele-head b{flex:1;font-size:13px}
.tele-head .btn{padding:4px 9px;font-size:11.5px}
.tele p{margin:0;max-height:26vh;overflow:auto;line-height:1.68;color:var(--ink)}
.tele-foot{margin-top:8px;font-size:11px;color:var(--ink-3)}
.tip{
  position:fixed;z-index:35;max-width:360px;background:var(--surface);border:1px solid var(--border-strong);
  border-radius:12px;box-shadow:var(--elev);padding:10px 12px;font-size:13px;pointer-events:none
}
.tip[hidden]{display:none}
.tip .tn{margin-top:6px;color:var(--green);font-size:12px;border-left:2px solid var(--green);padding-left:7px}
.tip .tk{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--ink-3);margin-bottom:3px}
@media (prefers-reduced-motion:reduce){.orbit-cw,.orbit-ccw,.draw,.beacon.rumor .rumor-ring,.dye.playing .pulse.lit,.scene.active{animation:none}.bubble{transition:none}}
@media print{header.bar,.honest-corner,.tele,.drawer,.scrim{display:none}body{overflow:visible}
.scene{display:grid!important;height:auto;page-break-after:always}.draw{animation:none}}
"""

_JS = """
(function(){
'use strict';
var P=JSON.parse(document.getElementById('showcase-data').textContent);
var B=document.body,cur=0,teleMode='solo',timer=null,remain=300,dyeTimers=[];
function $(id){return document.getElementById(id);}
function all(sel,root){return Array.prototype.slice.call((root||document).querySelectorAll(sel));}
var scenes=all('.scene'),nodes=all('.node'),segs=all('.seg');
var drawer=$('drawer'),scrim=$('scrim'),tele=$('teleprompter'),tip=$('tip');
function setAttr(k,v){B.setAttribute('data-'+k,v);syncToggles();}
function syncToggles(){
  all('[data-subject-btn]').forEach(function(b){b.classList.toggle('on',b.getAttribute('data-subject-btn')===B.getAttribute('data-subject'));});
  all('[data-round-btn]').forEach(function(b){b.classList.toggle('on',b.getAttribute('data-round-btn')===B.getAttribute('data-round'));});
  all('[data-metric-btn]').forEach(function(b){b.classList.toggle('on',b.getAttribute('data-metric-btn')===B.getAttribute('data-metric'));});
}
function go(i){
  cur=(i+scenes.length)%scenes.length;
  scenes.forEach(function(s,k){s.classList.toggle('active',k===cur);});
  nodes.forEach(function(n,k){n.classList.toggle('on',k===cur);n.classList.toggle('done',k<cur);});
  segs.forEach(function(s,k){s.classList.toggle('done',k<cur);});
  B.setAttribute('data-scene',String(cur));
  updateTele();
  if(cur===0){setTimeout(playClone,320);}
}
function smil(root){all('.smil',root).forEach(function(a){try{a.beginElement();}catch(e){}});}
function playClone(){var st=$('clone-stage');if(!st)return;st.classList.remove('run');smil(st);setTimeout(function(){st.classList.add('run');},1450);}
function visibleGroup(stageId,attrs){
  return all('#'+stageId+' > g').filter(function(g){
    return attrs.every(function(a){var v=g.getAttribute('data-only-'+a);return v===null||v===B.getAttribute('data-'+a);});
  })[0];
}
function stopDye(){dyeTimers.forEach(clearTimeout);dyeTimers=[];}
function playDye(){
  stopDye();
  var g=visibleGroup('dye-stage',['subject']);if(!g)return;
  var ind=$('dye-ind');
  g.classList.remove('played');g.classList.add('playing');
  all('.lit',g).forEach(function(el){el.classList.remove('lit');});
  for(var r=1;r<=8;r++){(function(r){dyeTimers.push(setTimeout(function(){
    all('.r'+r,g).forEach(function(el){el.classList.add('lit');});
    if(ind){ind.textContent='第 '+r+'/8 轮';}
    if(r===8){dyeTimers.push(setTimeout(function(){g.classList.add('played');},650));}
  },360+r*560));})(r);}
}
function resetDye(){stopDye();all('.dye').forEach(function(g){g.classList.remove('playing','played');});var ind=$('dye-ind');if(ind){ind.textContent='第 0/8 轮';}}
function playPulse(){var g=visibleGroup('track-stage',['subject','round']);if(g){smil(g);}}
function openDrawer(tab,anchor){
  drawer.setAttribute('data-tab',tab||'source');
  drawer.classList.toggle('wide',(tab||'source')==='source'&&cur===2);
  drawer.classList.add('open');drawer.setAttribute('aria-hidden','false');scrim.hidden=false;
  var body=$('dr-body');
  all('.hl',body).forEach(function(el){el.classList.remove('hl');});
  setTimeout(function(){
    if(anchor){var el=$(anchor);if(el){el.scrollIntoView({block:'center'});el.classList.add('hl');}}else{body.scrollTop=0;}
    $('dr-close').focus();
  },60);
}
function closeDrawer(){drawer.classList.remove('open');drawer.setAttribute('aria-hidden','true');scrim.hidden=true;}
function drawerOpen(){return drawer.classList.contains('open');}
function updateTele(){
  var sc=P.scenes[cur];
  $('tele-title').textContent='第 '+sc.code+' 幕 · '+sc.name;
  $('tele-window').textContent=sc.window;
  $('tele-text').textContent=teleMode==='solo'?sc.solo:sc.team;
  all('[data-tele]').forEach(function(b){b.classList.toggle('on',b.getAttribute('data-tele')===teleMode);});
}
function fmt(s){var a=Math.abs(s),m=Math.floor(a/60),r=a%60;return (s<0?'-':'')+(m<10?'0':'')+m+':'+(r<10?'0':'')+r;}
function renderTimer(){var t=$('timer');t.textContent=fmt(remain);t.classList.toggle('over',remain<0);
  $('pace').style.width=Math.min(100,Math.max(0,(300-remain)/300*100))+'%';$('btn-timer').textContent=timer?'暂停':'计时';}
function tickTimer(){remain-=1;renderTimer();}
function toggleTimer(){if(timer){clearInterval(timer);timer=null;}else{timer=setInterval(tickTimer,1000);}renderTimer();}
function toggleTheme(){var h=document.documentElement;var d=h.getAttribute('data-theme')==='dark';h.setAttribute('data-theme',d?'light':'dark');$('btn-theme').textContent=d?'暗场':'亮场';}
function showTip(html,ev){tip.innerHTML=html;tip.hidden=false;moveTip(ev);}
function moveTip(ev){var x=ev.clientX+16,y=ev.clientY+16;var w=tip.offsetWidth,h=tip.offsetHeight;
  if(x+w>window.innerWidth-8){x=ev.clientX-w-16;}if(y+h>window.innerHeight-8){y=ev.clientY-h-16;}tip.style.left=x+'px';tip.style.top=y+'px';}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
var EV={};P.evidence.forEach(function(e){EV[e.evidence_id]=e;});
var SUBJ={};P.subjects.forEach(function(s){SUBJ[s.subject_id]=s;});
function cardTip(key){var parts=key.split(':');var c=SUBJ[parts[0]].feed[Number(parts[1])];
  var rel={same:'同阵营',neutral:'中立',opposite:'对立'}[c.relation];
  var h='<div class="tk">'+esc(parts[0])+' · R'+c.round+'-'+c.slot+' · '+rel+(c.misleading?' · 含失真数字':'')+'</div><div>'+esc(c.text)+'</div>';
  c.evidence_ids.forEach(function(id){h+='<div class="tn">核查旁注（曝光时受试不可见）'+esc(id)+'：'+esc(EV[id].text)+'</div>';});
  return h;}
document.addEventListener('mouseover',function(ev){
  var t=ev.target.closest?ev.target.closest('[data-card],[data-tip]'):null;if(!t){tip.hidden=true;return;}
  if(t.hasAttribute('data-card')){showTip(cardTip(t.getAttribute('data-card')),ev);}else{showTip(esc(t.getAttribute('data-tip')),ev);}
});
document.addEventListener('mousemove',function(ev){if(!tip.hidden){moveTip(ev);}});
document.addEventListener('focusin',function(ev){var t=ev.target;if(t.hasAttribute&&t.hasAttribute('data-card')){var r=t.getBoundingClientRect();showTip(cardTip(t.getAttribute('data-card')),{clientX:r.right,clientY:r.bottom});}});
document.addEventListener('click',function(ev){
  var t=ev.target.closest?ev.target.closest('[data-go],[data-open],[data-act],[data-mirror],[data-subject-btn],[data-round-btn],[data-metric-btn],[data-tele],.dr-tab'):null;
  if(!t)return;
  if(t.hasAttribute('data-go')){go(Number(t.getAttribute('data-go')));}
  else if(t.classList.contains('dr-tab')){openDrawer(t.getAttribute('data-tab'));}
  else if(t.hasAttribute('data-open')){openDrawer(t.getAttribute('data-open'));}
  else if(t.hasAttribute('data-mirror')){openDrawer('source','mirror-'+t.getAttribute('data-mirror'));}
  else if(t.hasAttribute('data-subject-btn')){resetDye();setAttr('subject',t.getAttribute('data-subject-btn'));}
  else if(t.hasAttribute('data-round-btn')){setAttr('round',t.getAttribute('data-round-btn'));}
  else if(t.hasAttribute('data-metric-btn')){setAttr('metric',t.getAttribute('data-metric-btn'));}
  else if(t.hasAttribute('data-tele')){teleMode=t.getAttribute('data-tele');updateTele();}
  else if(t.hasAttribute('data-act')){var a=t.getAttribute('data-act');if(a==='clone'){playClone();}else if(a==='dye'){playDye();}else if(a==='pulse'){playPulse();}}
});
document.addEventListener('keydown',function(ev){
  var g=ev.target.closest?ev.target.closest('.clickable,[data-card]'):null;
  if(g&&(ev.key==='Enter')){g.dispatchEvent(new MouseEvent('click',{bubbles:true}));ev.preventDefault();return;}
  if(ev.key==='Escape'){closeDrawer();tip.hidden=true;return;}
  if(drawerOpen())return;
  if(ev.key==='ArrowRight'){go(cur+1);}
  else if(ev.key==='ArrowLeft'){go(cur-1);}
  else if(ev.key>='1'&&ev.key<='4'){go(Number(ev.key)-1);}
  else if(ev.key===' '||ev.code==='Space'){ev.preventDefault();toggleTimer();}
  else if(ev.key==='t'||ev.key==='T'){tele.classList.toggle('hidden');}
  else if(ev.key==='d'||ev.key==='D'){toggleTheme();}
});
$('dr-close').addEventListener('click',closeDrawer);
scrim.addEventListener('click',closeDrawer);
$('btn-timer').addEventListener('click',toggleTimer);
$('btn-theme').addEventListener('click',toggleTheme);
syncToggles();renderTimer();go(0);
})();
"""


def _scene_head(scene: dict, controls: str) -> str:
    return (
        '<div class="scene-head"><div>'
        f'<div class="eyebrow-row"><span class="scene-tag">SCENE {scene["code"]}</span>'
        f'<span class="win">{_e(scene["name"])} · {_e(scene["window"])}</span></div>'
        f'<h2>{_e(scene["title"])}</h2>'
        f'<p class="lede">{_e(scene["lede"])}</p></div>'
        f'<div class="ctrls">{controls}</div></div>'
    )


def _subject_btns(p: dict) -> str:
    btns = "".join(f'<button type="button" class="btn" data-subject-btn="{s["subject_id"]}">{_e(s["label"])}</button>'
                   for s in p["subjects"])
    return f'<div class="seg-pill">{btns}</div>'


def _body(p: dict) -> str:
    sc = p["scenes"]
    nav = []
    for k, s in enumerate(sc):
        nav.append(f'<button type="button" class="node" data-go="{k}" title="{_e(s["title"])}">'
                   f'<span class="nc">{s["code"]}</span>{_e(s["name"])}</button>')
        if k < len(sc) - 1:
            nav.append('<span class="seg"><i></i></span>')
    header = (
        '<header class="bar"><div class="brand">'
        '<svg class="logo" viewBox="0 0 22 22" aria-hidden="true">'
        '<circle class="logo-ring" cx="11" cy="11" r="9"/>'
        '<path class="logo-lens" d="M11 4 Q16 11 11 18 Q6 11 11 4 Z"/></svg>'
        '<b>破茧助手</b><span>实验交互沙盘</span></div>'
        f'<nav class="prog" aria-label="四幕进度">{"".join(nav)}</nav>'
        '<div class="tools">'
        '<div class="timer-wrap"><span class="timer" id="timer">05:00</span>'
        '<button type="button" class="btn" id="btn-timer">计时</button></div>'
        '<button type="button" class="btn" data-open="source">原文对照</button>'
        '<button type="button" class="btn" data-open="qa">答辩问答</button>'
        '<button type="button" class="btn" data-open="boundary">科研边界</button>'
        '<a class="btn" href="./dashboard.html">审计后台 ↗</a>'
        '<button type="button" class="btn" id="btn-theme">暗场</button></div>'
        '<div class="pace"><i id="pace"></i></div></header>'
    )
    rounds = ('<div class="seg-pill">'
              + "".join(f'<button type="button" class="btn" data-round-btn="{k}">第 {k} 轮：{_e(v)}</button>'
                        for k, v in _ROUND_THEMES.items())
              + "</div>")
    metrics = ('<div class="seg-pill">'
               '<button type="button" class="btn" data-metric-btn="acc">事实真相识别率 %</button>'
               '<button type="button" class="btn" data-metric-btn="brier">认知判断误差 Brier</button>'
               '</div>')
    scenes = [
        (_scene_head(sc[0], '<button type="button" class="btn btn-accent" data-act="clone">演示克隆分流</button>'
                            '<button type="button" class="btn" data-open="boundary">规模明细</button>'), _svg_clone(p)),
        (_scene_head(sc[1], _subject_btns(p)
                            + '<button type="button" class="btn btn-accent" data-act="dye">播放 8 轮染色脉冲</button>'
                            + '<span class="ind" id="dye-ind">第 0/8 轮</span>'
                            + '<button type="button" class="btn" data-open="source">完整发帖原文</button>'), _svg_dye(p)),
        (_scene_head(sc[2], _subject_btns(p) + rounds
                            + '<button type="button" class="btn btn-accent" data-act="pulse">播放干预脉冲</button>'
                            + '<button type="button" class="btn" data-open="source">三轨完整原文 ↗</button>'),
         _svg_tracks(p)),
        (_scene_head(sc[3], metrics + '<button type="button" class="btn" data-open="source">终局数值表</button>'),
         _svg_final(p)),
    ]
    sections = "".join(f'<section class="scene{" active" if k == 0 else ""}" id="scene-{k}" aria-label="{_e(sc[k]["name"])}">'
                       f'{head}<div class="stage-box">{svg}</div></section>' for k, (head, svg) in enumerate(scenes))
    tele = ('<aside id="teleprompter" class="tele hidden" aria-live="polite"><div class="tele-head"><b id="tele-title"></b>'
            '<span class="mono" id="tele-window"></span>'
            '<button type="button" class="btn" data-tele="solo">单人</button>'
            '<button type="button" class="btn" data-tele="team">五人接力</button></div>'
            '<p id="tele-text"></p><div class="tele-foot">T 隐藏 · ←/→ 或 1–4 切幕 · 空格计时 · D 主题 · Esc 关抽屉</div></aside>')
    corner = f'<button type="button" id="honest-corner" class="honest-corner" data-open="boundary">{_e(p["honest_badge"])}</button>'
    return (header + f'<main class="stage">{sections}</main>' + corner + tele
            + '<div id="tip" class="tip" hidden></div>' + _drawer(p))


def _render_document(payload: dict) -> str:
    """纯渲染器：校验时据此重建整页并逐字节比对。"""
    embedded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    return ("<!DOCTYPE html>\n"
            '<html lang="zh-CN" data-theme="light">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            "<title>破茧助手 · 五分钟实验交互沙盘</title>\n"
            f"<style>{_CSS}</style>\n</head>\n"
            '<body data-scene="0" data-subject="S01" data-round="1" data-metric="acc">\n'
            f"{_body(payload)}\n"
            f'<script id="showcase-data" type="application/json">{embedded}</script>\n'
            f"<script>{_JS}</script>\n</body>\n</html>\n")


def render_five_minute_showcase(output_path: Path = DEFAULT_SHOWCASE_PATH,
                                dataset_path: Path = DEFAULT_DATASET_PATH) -> dict:
    """生成单文件离线自包含的 showcase_5min.html（项目内只允许写默认路径）。"""
    out = Path(output_path).resolve()
    _need(out != Path(dataset_path).resolve(), "output_is_source_dataset")
    _need(not out.is_relative_to(ROOT.resolve()) or out == DEFAULT_SHOWCASE_PATH.resolve(),
          "output_would_modify_project_artifact")
    payload = build_showcase_payload(dataset_path)
    doc = _render_document(payload)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8", newline="\n")
    return {"output_path": str(out), "bytes": out.stat().st_size,
            "html_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
            "showcase_sha256": payload["showcase_sha256"], "source_dataset_sha256": payload["source_dataset_sha256"]}


# ============================================================ 核验
def _strip_data(raw_html: str) -> str:
    return re.sub(r'<script id="showcase-data"[^>]*>.*?</script>', "", raw_html, flags=re.S)


def _scene_text_chars(section_html: str) -> int:
    body = re.sub(r"<svg\b.*?</svg>", "", section_html, flags=re.S)
    body = re.sub(r"<h2\b.*?</h2>|<button\b.*?</button>|<a\b.*?</a>", "", body, flags=re.S)
    return len(CJK.findall(re.sub(r"<[^>]+>", "", body)))


def _ui_sandbox_audit(raw_html: str) -> dict:
    """反 AI 味、反粗黑简笔画与沙盘完整性硬门禁（只看标记，不依赖浏览器）。"""
    markup = _strip_data(raw_html)
    _need(len(EMOJI.findall(markup)) == 0, "ui_emoji_forbidden")
    _need(GRADIENT_TEXT.search(markup) is None, "ui_gradient_text_forbidden")
    _need(re.search(r"linear-gradient\([^)]*#60f", markup, re.I) is None
          and re.search(r"\.btn\s*\{[^}]*linear-gradient", markup, re.I) is None,
          "ui_gradient_button_forbidden")
    _need(HARSH_BLOCK_SHADOW.search(markup) is None, "ui_harsh_shadow_forbidden")
    _need(STICK_FIGURE_PATH.search(markup) is None, "ui_stick_figure_forbidden")
    _need("<linearGradient" in markup and "<radialGradient" in markup, "ui_optical_defs_missing")
    tele = re.search(r'<aside id="teleprompter" class="([^"]*)"', markup)
    _need(tele is not None and "hidden" in tele.group(1).split(), "ui_teleprompter_not_hidden")
    drawer = re.search(r'<aside id="drawer" class="([^"]*)" aria-hidden="([^"]*)"', markup)
    _need(drawer is not None and drawer.group(1).split() == ["drawer"] and drawer.group(2) == "true",
          "ui_drawer_not_closed")
    _need(re.search(r'<div id="scrim" class="scrim" hidden>', markup) is not None, "ui_scrim_not_hidden")
    stage_ids = ("clone-stage", "dye-stage", "track-stage", "final-stage")
    lede_chars = []
    for idx in range(4):
        m = re.search(rf'<section class="scene[^"]*" id="scene-{idx}"[^>]*>(.*?)</section>', markup, flags=re.S)
        _need(m is not None, f"ui_scene_{idx}_missing")
        section = m.group(1)
        _need(f'<div class="stage-box"><svg class="sandbox" id="{stage_ids[idx]}"' in section,
              f"ui_scene_{idx}_sandbox_missing")
        _need(section.count("<svg") == 1, f"ui_scene_{idx}_svg_count")
        chars = _scene_text_chars(section)
        _need(chars <= LEDE_LIMIT, f"ui_scene_{idx}_text_over_limit")
        lede_chars.append(chars)
    _need(re.search(r'id="honest-corner"', markup) is not None, "ui_honest_corner_missing")
    return {"emoji_hits": 0, "gradient_text": 0, "harsh_block_shadow": 0, "stick_figure": 0,
            "optical_defs": True, "teleprompter_hidden": True, "drawer_closed": True,
            "scene_text_chars": lede_chars, "sandbox_stages": list(stage_ids)}


def _payload_truth_audit(payload: dict, dataset: dict, manifest: dict) -> dict:
    """嵌入数据与原始来源逐项比对（发言、mirror、字数、配对、哈希、双轨、终局、披露）。"""
    closed = dataset["closed_world_pilot"]
    src = {s["subject"]: s for s in closed["subjects"]}
    checked = 0
    for subj in payload["subjects"]:
        sid, s = subj["subject_id"], src[subj["subject_id"]]
        _need(subj["speech"] == s["speech"], "truth_speech_mismatch")
        _need([it["text"] for it in subj["mirror"]["items"]] == [it["text"] for it in s["mirror_items"]],
              "truth_mirror_mismatch")
        _need(all(h in s["speech"] for h in subj["speech_highlights"] + subj["bubbles"]), "truth_highlight_not_verbatim")
        _need(subj["parent_snapshot_sha256"] == manifest["parent_t1_core_sha256"][sid]
              and len(subj["parent_snapshot_sha256"]) == 64, "truth_parent_hash_mismatch")
        for r_key, rc in subj["rounds"].items():
            for arm in ("I4", "I5"):
                _need(rc[arm.lower() + "_chars"] == len(manifest["interventions"][sid][arm][r_key]["visible_text"]),
                      "truth_chars_mismatch")
                _need(rc[arm.lower() + "_addition"] == s[arm.lower() + "_additions"][r_key], "truth_addition_mismatch")
            _need(all(rc["pair_checks"][k] is True for k in PAIR_CHECKS) and len(rc["pair_checks"]) == 7,
                  "truth_pair_checks")
            for arm in ARMS:
                _need(rc["responses"][arm] == s["intervention_responses"][arm][int(r_key) - 1], "truth_response_mismatch")
                _need(rc["bubbles"][arm] in rc["responses"][arm] and len(rc["bubbles"][arm]) <= BUBBLE_LIMIT,
                      "truth_bubble_not_verbatim")
            checked += 1
    sb = {r["arm"]: r for r in payload["scoreboard"]}
    _need(sb["I4"]["bs_t3"] == sb["I5"]["bs_t3"] == 0.0025, "truth_primary_endpoint_tie")
    _need(sb["I4"]["transfer_correct_t3"] == 4.0 and sb["I5"]["transfer_correct_t3"] == 3.0, "truth_transfer")
    _need(sb["I4"]["o_t3"] == 58.3 and sb["I5"]["o_t3"] == 33.3, "truth_omission_disclosure")
    _need(payload["synth_track"]["acc_series"]["I0"] == [50.0] * 4
          and payload["real_track"]["acc_series"]["I4"][0] == 100.0, "truth_dual_track")
    q5 = payload["qa_cards"][4]["a"]
    _need("XL_E03" in q5 and "XL_E06" in q5, "truth_q5_review_flag_missing")
    flags = [rc["review_flag"] for s in payload["subjects"] for rc in s["rounds"].values() if rc["review_flag"]]
    _need(len(flags) == 1 and flags[0]["cited"] == "XL_E03" and flags[0]["fact_source"] == "XL_E06", "truth_review_flag")
    for token in ("mimo-v2.6-flash", "n=2", "§5.4", "formal_eligible=False"):
        _need(token in payload["honest_badge"], "truth_honest_badge_" + token)
    return {"rounds_checked": checked, "primary_endpoint_tie": 0.0025, "transfer": [4.0, 3.0], "omission": [58.3, 33.3]}


def verify_five_minute_showcase(showcase_path: Path = DEFAULT_SHOWCASE_PATH,
                                dataset_path: Path = DEFAULT_DATASET_PATH) -> dict:
    """独立重算并核验：数据一致、反 AI 味门禁、零外链、零凭据、整页逐字节可重现。"""
    path = Path(showcase_path)
    _need(path.is_file(), "showcase_html_missing")
    raw_html = path.read_text(encoding="utf-8")
    _need(db._CREDENTIAL.search(raw_html) is None, "credential_leak_detected")
    match = re.search(r'<script id="showcase-data" type="application/json">(.*?)</script>', raw_html, flags=re.S)
    _need(match is not None, "embedded_data_missing")
    embedded = json.loads(match.group(1))
    recomputed = build_showcase_payload(dataset_path)
    _need(embedded.get("contract") == SHOWCASE_CONTRACT, "contract_mismatch")
    _need(embedded.get("showcase_sha256") == recomputed["showcase_sha256"], "showcase_hash_mismatch")
    _need(embedded == recomputed, "embedded_payload_mismatch")
    stripped = _strip_data(raw_html)
    for pattern in db._FORBIDDEN_LOAD:
        _need(pattern.search(stripped) is None, "external_resource_forbidden")
    _need("http://" not in stripped and "https://" not in stripped, "external_url_in_markup")
    _need(raw_html.count("<script") == 2 and raw_html.count("<style") == 1, "unexpected_script_or_style_count")
    ui = _ui_sandbox_audit(raw_html)
    dataset = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    manifest = json.loads(CLOSED_MANIFEST_PATH.read_text(encoding="utf-8"))
    truth = _payload_truth_audit(embedded, dataset, manifest)
    _need(html.escape(recomputed["honest_badge"]) in raw_html, "honest_badge_missing")
    _need(path.read_bytes() == _render_document(recomputed).encode("utf-8"), "html_document_mismatch")
    return {"passed": True, "contract": SHOWCASE_CONTRACT, "scenes": len(recomputed["scenes"]),
            "subjects": len(recomputed["subjects"]), "showcase_sha256": recomputed["showcase_sha256"],
            "source_dataset_sha256": recomputed["source_dataset_sha256"],
            "html_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "ui_sandbox_audit": ui,
            "truth_audit": truth}


if __name__ == "__main__":
    info = render_five_minute_showcase()
    check = verify_five_minute_showcase()
    print(json.dumps({"render": info, "verify": check, "passed": check["passed"], "contract": check["contract"]},
                     ensure_ascii=False, indent=2))
