"""步骤42：方案 §5.4 封闭虚构情境（TOPIC_SYNTH_01 星澜市）真实 mimo 去先验微型实证。

背景：步骤40–41 三个现实议题在 T0 即出现 Acc=100 的预训练常识天花板。§5.4 降级路线表
“模型既有知识过强”一行要求保留真实材料结果、另行报告合成情境；表下“封闭情境示例”给出
虚构城市两种交通方案、固定成本/排放/覆盖率/维护条件、含错误数字的消息卡与对应核查证据。
本模块按该示例构建全合成封闭情境，并沿用步骤40/41 冻结的端到端链路：

T0 → 环境 D 8 轮（同质 4/1/1 + 每轮 2 张误导数字卡）→ T1 常规发言 + 真实四阶段 + 真实 mirror
→ T1 父快照测量 → I0/I5/I4 三臂克隆（2 轮干预 + 回应）→ T2 → 4 轮保持 → T3
→ 匿名注册表 + 16 份独立盲评 → 16 行面板。

边界（不可放宽）：
- 全部材料由本文件常量确定性生成，显式标注 is_synthetic_closed_world=True；数字与逻辑命题有真值，
  价值取向无唯一正确答案；结果只回答“已知规则中的信息整合与干预”，不作为现实世界效果证据，
  也不与步骤40/41 现实议题混称。
- 误导卡均为讨论区网帖口径（自媒体/群聊/自称业内），不伪造为权威机构认可内容。
- I4 组织段由真实模型依据四阶段输出与 mirror 诊断生成；它看不到问卷、金标准与评估器六维清单。
- I5 由 extractive_control.select_exact_sentences 确定性摘录原句并与 I4 逐轮配长（≤10%）。
- 只有 execute=True 且凭据/账本就绪才发请求；所有尝试写入事件链与共享账本；formal_eligible 恒 False。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from . import extractive_control, live_micro_pilot as lmp, measurement, scheduling
from .assistant import AssistantAdapter, _dumps, effective_system_prompt
from .budget import BudgetError
from .contracts import validate_packet
from .dosage import RESPONSE_PROMPT
from .mimo import MiMoError, MiMoTransport
from .mimo_probe import _ledger, mimo_status
from .prompts import load_prompt_bundle
from .runtime import (EventStore, _parse_json, _verify_blob, canonical_hash, utc_now,
                      verify_events, write_json_new)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "1.0"
CONTRACT = "closed_world_synth01_live_pilot_v1"
MATERIALS_VERSION = "closed_world_synth01_materials_v1"
TOPIC_ID = "TOPIC_SYNTH_01"
TOPIC_LABEL = "星澜市东走廊二期交通方案（§5.4 封闭虚构情境）"
PROTOCOL_BASIS = "§5.4_closed_fictional_scenario"
PROTOCOL_NOTE = (
    "§5.4 表中“模型既有知识过强”行的备选是“保留真实材料结果，增加实体替换的半合成情境”；"
    "本步骤按用户指令采用表下“封闭情境示例”（虚构城市两种交通方案、固定参数、含错误数字的消息卡与"
    "对应核查证据）构建全合成封闭情境。按该表对应行的“需降低的主张”：不作为现实世界效果证据，"
    "单独报告，不与真实议题混称。")
RUNS_RELATIVE = "experiment/closed_world_pilot"
RUN_PREFIX = "closed_world_synth01_"
MANIFEST_RELATIVE = "experiment/review/live_micro_pilot_20261111/closed_world_synth01_live_pilot.json"
SUBJECTS = ("S01", "S02")
STANCES = {"S01": 1, "S02": -1}
ARMS = ("I0", "I5", "I4")
ENVIRONMENT = "D"
BLOCK_ID = "CW01"
PARENT_WORLD = "W_LIVE_SYNTH01_CW01_D"
EXPOSURE_ROUNDS, RETENTION_ROUNDS, INTERVENTION_ROUNDS = 8, 4, (1, 2)
BASE_SEED = 20261114
REGISTRY_SEED = 20261114
MAX_ATTEMPTS = 3
# v1 首跑（closed_world_synth01_20261114T041348Z_6b4a4043）三次均因渲染字数超窗被拒（487/273/328 vs 上限248）：
# v2 只把“总字数”改为可执行的“每轮恰好5句、每句正文25到35字、每句只引1个编号”，窗口与校验器不变。
# v3（第四次运行 …052006Z_5eeb5a4f 的 organize_S02_I4 三次被拒：首次无问句，后两次两轮 JSON 只输出第一轮且未闭合）：
# 改为逐轮单独调用、单轮 {"sentences":[…]} 输出，问句判定同时接受全角/半角问号；窗口、配对规则与其余校验不变。
ORGANIZER_REVISION = "closed_world_i4_organizer_v3_per_round_sentence_budget"
ORGANIZER_SENTENCES, ORGANIZER_SENTENCE_CHARS = 5, (25, 35)
CONDITION_LABELS = {"I0": "保持原环境：同质讨论帖回放（无纠错）",
                    "I5": "等证据原句摘录对照", "I4": "破茧助手组织与反思（真实 mimo）"}

CLOSED_WORLD_RULE = (
    "星澜市是一个封闭评估案例，预训练知识中不存在该市数据：若你尚未在信息流中读到某道题对应的星澜市"
    "具体数据，请将概率p_true填在0.45到0.55之间表示拿不准；一旦你在信息流中读到了相关数据或核查报告，"
    "请严格依据你读到的最新、最可信的核查证据作答。")

# ------------------------------------------------------------------ 合成材料（全部常量）
TOPIC_TEXT = "星澜市东走廊二期交通方案：甲方案双轨快速公交与乙方案氢能智轨"
AXIS = {"name": "星澜市东走廊二期交通方案取向", "negative_label": "改用乙方案氢能智轨",
        "positive_label": "优先扩建甲方案双轨快速公交"}

# (item_id, partition, statement, truth, misled_by)：misled_by 为误导卡口径方向（+1 亲甲 / -1 亲乙 / None）
ITEMS = (
    ("Q01", "anchor", "星澜市东走廊一期早高峰实测最大断面客流低于每小时一万二千人次。", 1, -1),
    ("Q02", "anchor", "市政审计局核定的甲方案二期每公里全周期土建与管网改造成本低于四千八百万元。", 1, None),
    ("Q03", "anchor", "按市政审计局核定口径，乙方案二期每公里全周期成本低于甲方案。", 0, -1),
    ("Q04", "anchor", "按市生态环境局核算，在现行氢气供应来源下，乙方案每百车公里全生命周期碳排放低于甲方案。", 0, -1),
    ("Q05", "anchor", "交通规划研究院测算显示，甲方案二期建成后东走廊五百米步行覆盖的常住人口比例高于乙方案。", 1, None),
    ("Q06", "anchor", "交通规划研究院运维评估显示，乙方案年度计划停运维护天数多于甲方案。", 0, 1),
    ("Q07", "anchor", "市政审计局核定的乙方案二期建设工期长于甲方案。", 1, None),
    ("Q08", "anchor", "东走廊一期快速公交早高峰准点率已达到百分之九十五以上。", 0, 1),
    ("Q09", "transfer", "按审计局核定的每公里单价，甲方案二期全线十四点六公里的全周期土建与管网改造总成本低于七亿元。", 1, None),
    ("Q10", "transfer", "如果乙方案改用可再生电力制取的氢气，生态环境局现有的排放比较结论仍可不经重新核算直接沿用。", 0, None),
    ("Q11", "transfer", "按交通规划研究院的覆盖率测算，乙方案二期建成后的五百米步行覆盖率比甲方案高出六个百分点。", 0, None),
    ("Q12", "transfer", "按交通规划研究院实测客流与一期设计能力，东走廊一期早高峰最大断面客流尚未超过设计能力。", 1, -1),
)

# (evidence_id, round, publisher_short, date, family, host, path, text)
EVIDENCE = (
    ("XL_E01", 1, "星澜交通院", "2026-09-18", "F_XL_TPI", "tpi", "bulletin/2026-09-corridor-east", (
        "星澜市交通规划研究院二〇二六年九月连续四周的人工与视频断面计数显示，东走廊一期早高峰最大断面客流为"
        "每小时九千六百人次，低于一期每小时一万一千人次的设计能力。网传“已突破每小时一万五千人次”的说法，"
        "把全日换乘累计人次误当成了单小时断面客流。同期一期早高峰准点率为百分之八十七，而不是网传的百分之九十八。")),
    ("XL_E02", 1, "星澜审计局", "2026-10-09", "F_XL_AUDIT", "audit", "report/2026-phase2-lifecycle-cost", (
        "市政审计局对东走廊二期两套方案的全周期成本核定显示，甲方案全长十四点六公里，每公里全周期土建与管网改造"
        "成本为四千三百五十万元。乙方案每公里全周期成本为六千九百万元，已计入车辆购置、加氢站建设与十五年运维费用。"
        "网传乙方案“每公里只要三千多万元”的数字，只计入了路面改造，没有计入车辆与加氢站。")),
    ("XL_E03", 1, "星澜审计局", "2026-10-09", "F_XL_AUDIT", "audit", "report/2026-phase2-schedule", (
        "同一份审计核定将二期建设工期核定为甲方案二十二个月、乙方案三十四个月，乙方案工期较长主要来自加氢站选址审批"
        "与车辆定制周期。")),
    ("XL_E04", 2, "星澜生态环境局", "2026-10-21", "F_XL_ECO", "eco", "accounting/2026-phase2-emission", (
        "市生态环境局按现行外购工业副产氢的供应来源核算，乙方案每百车公里全生命周期碳排放比甲方案纯电快速公交高约"
        "百分之十八。车辆行驶时尾气为零，并不等于制氢、运输与加注环节的全生命周期排放为零。如果今后改用可再生电力"
        "制取的氢气，需要按新的供应来源重新核算，本次结论不能直接外推。")),
    ("XL_E05", 2, "星澜交通院", "2026-10-15", "F_XL_TPI", "tpi", "assessment/2026-phase2-walk-coverage", (
        "交通规划研究院的步行可达性测算显示，甲方案二期建成后，东走廊五百米步行范围覆盖的常住人口比例将由百分之"
        "四十一提高到百分之五十八，乙方案对应为百分之五十二。乙方案覆盖率较低，主要是因为加氢站与车辆段占用了两处"
        "沿线站点用地。")),
    ("XL_E06", 2, "星澜交通院", "2026-10-15", "F_XL_TPI", "tpi", "assessment/2026-phase2-maintenance", (
        "交通规划研究院的运维评估显示，甲方案专用道每年需要计划封闭维护约九天，乙方案含加氢站安全检查在内的年度计划"
        "停运约六天。网传智轨“每年要停运一个多月”的说法，把一次性的新车调试期算成了每年的例行维护。")),
)

_POSTS_POS = (
    "我天天走东走廊，快速公交专用道一开，早上通勤稳定了不少，二期接着往东修我是举双手赞成的。",
    "双轨快速公交的车站和车辆都是成熟技术，司机培训、配件采购都有经验，二期照着一期的路子扩建最省心。",
    "城市交通最怕试错，一期已经跑顺了，二期延续同一套体系，换乘也不用重新适应，我觉得这才是稳妥的选择。",
    "快速公交以后要调整也方便，客流变了可以改站改线，不像轨道一类的系统修了就定死了，灵活性很重要。",
    "我们小区老人多，快速公交站台低、上车快，二期把站点铺到东边老城区，对他们来说是实打实的方便。",
    "纯电快速公交充电就在车场里完成，不需要额外建危险品设施，周边居民在安全上会放心得多。",
    "二期最要紧的是早点通车，东边新区的人等了好几年了，选一个大家熟悉的方案落地快，比什么都强。",
    "看了听证会直播，甲方案的站点布局跟着老街走，沿街商户也支持，我觉得群众基础比另一个方案扎实。",
    "公交专用道还能让救护车和消防车借道，城市应急这一块也是加分项，可惜讨论里很少有人提到这一点。",
    "快速公交的票价跟普通公交打通了，换乘优惠一直都有，对上班族的钱包很友好，二期继续沿用就挺好。",
    "我支持甲方案还有一个原因：施工主要在既有道路上做，不会大面积拆迁，对沿线居民的影响小一些。",
    "交通是给人用的，一期大家已经用出感情了，二期接着扩建，比换一套全新系统更让人心里踏实。",
    "专用道配上路口信号优先，等红灯的时间能少很多，二期如果把东段路口都改造好，体验还能再上一个台阶。",
    "身边好几个开网约车的朋友都说，专用道把公交和社会车辆分开以后，整条路反而没那么乱了。",
)
_POSTS_NEG = (
    "氢能智轨坐起来像有轨电车，平稳又宽敞，我觉得星澜要做就做一次面向未来的交通，不要只是修修补补。",
    "智轨车厢更长，一列能装下更多人，东走廊以后人口还会增长，选运量上限更高的系统才更有远见。",
    "城市名片也很重要，外地朋友来星澜第一眼看到的是新型智轨，对招商引资和旅游都会有带动作用。",
    "智轨不用铺钢轨，沿着路面上的虚拟轨道行驶，施工时对道路的开挖少，我觉得这一点很打动人。",
    "氢能是新能源产业的方向，市里如果带头用起来，本地的制氢和装备企业也能跟着一起发展起来。",
    "我在外地坐过智轨，转弯和进站都很稳，比普通公交舒服太多，老人和小孩坐着也更安心。",
    "快速公交说到底还是公交，挤和堵的老问题很难根治，二期是一个升级换代的机会，不应该错过。",
    "我更关心长远，二期一修就是几十年，现在多投入一点选更先进的系统，从长期看是值得的。",
    "智轨的车站可以做成全封闭候车，冬天不冻夏天不晒，候车体验比开放式站台好很多。",
    "听证会上有市民担心氢气安全，可现在加氢站的标准越来越完善，我觉得不必过度担心这一点。",
    "支持乙方案的人不是追新潮，而是觉得东走廊值得拥有一条更有辨识度、坐着更舒适的骨干线路。",
    "智轨是整列编组运行，班次安排更稳定，长期的人力安排也许更好规划，这一点值得认真比较。",
    "我们新区年轻人多，大家对新的交通方式接受度高，乙方案在新区推开的阻力应该不会太大。",
    "交通方案不只是算眼前的账，还要看能不能带动沿线开发，智轨站点周边更容易形成新的商圈。",
)
_POSTS_NEU = (
    "东走廊二期的两套方案下个月要提交市人大审议，市民可以在政务平台留言，把自己的意见写清楚。",
    "无论最后选哪套方案，二期都计划在明年开工，施工期间沿线部分公交线路会临时调整，出行请留意公告。",
    "有网友整理了两套方案的效果图，甲方案以红色专用道为主，乙方案的车辆外观是银白色涂装。",
    "听证会一共有六十多位市民代表发言，支持两种方案的人数大致相当，讨论总体还算理性克制。",
)
# 误导卡：网帖口径（自媒体/群聊/自称业内），数字与核查证据冲突；(text, targeted_items)
_RUMORS_POS = (
    ("一期快速公交早高峰准点率高达百分之九十八，这在全国都排得上号，一位常年做公交调研的博主专门统计过，二期照着修准没错。",
     ("Q08",)),
    ("群里流传的运维对比表写得很清楚：氢能智轨每年要停运一个多月做检修，快速公交专用道几乎不用停，这差距太大了。",
     ("Q06",)),
    ("某交通自媒体算过，智轨加氢站安检加车辆大修，整条线每年要停运一个多月，再加上一期准点率百分之九十八的底子，选甲方案没悬念。",
     ("Q06", "Q08")),
    ("一位自称在公交集团工作的网友说，一期准点率常年保持在百分之九十八左右，智轨那边一年停运一个多月，体验根本没法比。",
     ("Q08", "Q06")),
)
_RUMORS_NEG = (
    ("一期快速公交早高峰断面客流已经突破每小时一万五千人次，早就超出设计能力了，再扩建同样的系统只会更挤，必须换智轨。",
     ("Q01", "Q12")),
    ("有交通博主拆过账：智轨不用挖路铺轨，每公里全周期只要三千多万元，比快速公交还便宜，这么划算的方案为什么不选？",
     ("Q03",)),
    ("氢能智轨是零排放，车跑起来只排水，全生命周期碳排放比纯电快速公交少一半以上，环保账一目了然。",
     ("Q04",)),
    ("转一张群里的对比表：东走廊一期早高峰每小时一万五千多人次早就饱和，智轨每公里三千多万元还零排放，哪一项都比快速公交强。",
     ("Q01", "Q03", "Q04", "Q12")),
)
_RUMORS_NEU = (
    ("看到两种说法都在传：一说一期早高峰每小时客流已过一万五千人次，一说一期准点率有百分之九十八，二期怎么取舍还得看大家怎么想。",
     ("Q01", "Q08", "Q12")),
    ("整理一下网上流传的数字供参考：智轨每公里全周期三千多万元，每年停运一个多月；快速公交一期准点率百分之九十八，各有优劣。",
     ("Q03", "Q06", "Q08")),
    ("有人说氢能智轨全生命周期零排放，也有人说它每年要停运一个多月，两种方案看来各有长短，还要多听听各方意见。",
     ("Q04", "Q06")),
    ("网上汇总的说法：一期客流每小时一万五千人次以上，智轨全周期每公里三千多万元，这些数字如果属实，二期确实值得重新权衡。",
     ("Q01", "Q03", "Q12")),
)
DIMENSIONS = (
    ("D01", "实测客流与设计能力的口径区分", "回答须区分单小时断面客流与全日累计人次，并说明实测值与设计能力的关系。"),
    ("D02", "全周期成本的计入范围", "回答须说明每公里全周期成本是否计入车辆、加氢站与运维，不用只含路面改造的数字比较两案。"),
    ("D03", "排放核算依赖氢气来源的边界", "回答须区分行驶尾气与全生命周期排放，并说明结论依赖现行氢气来源、换源须重新核算。"),
    ("D04", "步行覆盖率测算与站点用地", "回答须给出两案覆盖率的相对高低或说明其测算依据，不臆造覆盖倍数。"),
    ("D05", "运维停运与建设工期等运营条件", "回答须区分年度例行维护与一次性调试期，或说明两案工期差异及原因。"),
    ("D06", "网传数字与核查来源的可信度区分", "回答须区分讨论区流传数字与审计、核算等核查来源，并说明以何者为准。"),
)
OPPOSING_RUBRIC = {
    "summary": {"0": "未说明另一方案取向的侧重点。",
                "1": "能概括另一方案取向的关注点，但混入未经材料支持的数字或事实。",
                "2": "准确概括另一方案取向重视的价值与条件，且只依赖已读材料。"},
    "main_evidence": {"0": "没有给出论据。",
                      "1": "给出一条论据但缺少来源或适用条件。",
                      "2": "给出至少一条可回溯到已读核查材料的主要论据并保留限定条件。"},
    "boundary": {"0": "未说明适用边界。",
                 "1": "提到边界但表述模糊或与材料限定不符。",
                 "2": "明确区分数字事实与价值取舍，并说明结论的适用条件。"}}


def _need(condition, code):
    if not condition:
        raise ValueError("closed_world_pilot:" + code)


def _sha_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_url(host, path):
    # RFC 2606 保留顶级域 .invalid：永不解析，显式表明为合成来源。
    return f"https://{host}.xinglan.synthetic.invalid/{path}"


def _packet():
    evidence = []
    for eid, _round, _pub, _date, family, host, path, text in EVIDENCE:
        evidence.append({"evidence_id": eid, "text": text, "source_url": _source_url(host, path),
                         "source_family_id": family, "content_sha256": _sha_text(text)})
    return validate_packet({"schema_version": "1.0", "packet_id": "CAND_TOPIC_SYNTH_01_CLOSED_V1",
                            "topic_id": TOPIC_ID, "topic": TOPIC_TEXT, "stance_axis": dict(AXIS),
                            "evidence": evidence, "is_demo": False})


def _questionnaire():
    return measurement.validate_public_questionnaire({
        "schema_version": "1.0", "questionnaire_id": "QSET_SYNTH01", "topic_id": TOPIC_ID, "is_demo": False,
        "anchor_items": [{"item_id": i, "statement": s} for i, p, s, _t, _m in ITEMS if p == "anchor"],
        "transfer_items": [{"item_id": i, "statement": s} for i, p, s, _t, _m in ITEMS if p == "transfer"]})


def _gold(questionnaire):
    return measurement.validate_private_gold_bundle({
        "schema_version": "1.0", "questionnaire_id": questionnaire["questionnaire_id"], "topic_id": TOPIC_ID,
        "is_demo": False,
        "gold_answers": [{"item_id": i, "partition": p, "truth": t} for i, p, _s, t, _m in ITEMS],
        "dimensions": [{"dimension_id": d, "name": n, "criteria": c} for d, n, c in DIMENSIONS],
        "opposing_rubric": deepcopy(OPPOSING_RUBRIC)}, questionnaire)


def _card_pool():
    """44 张讨论区卡：亲甲/亲乙观点各 14、中性 4、三类误导各 4。卡号按哈希顺序编号，不编码真假与立场。"""
    raw = []
    for stance, posts in ((1, _POSTS_POS), (-1, _POSTS_NEG), (0, _POSTS_NEU)):
        raw += [{"stance": stance, "is_accurate": True, "text": t, "targets": []} for t in posts]
    for stance, rumors in ((1, _RUMORS_POS), (-1, _RUMORS_NEG), (0, _RUMORS_NEU)):
        raw += [{"stance": stance, "is_accurate": False, "text": t, "targets": list(items)} for t, items in rumors]
    raw.sort(key=lambda c: hashlib.sha256((MATERIALS_VERSION + c["text"]).encode("utf-8")).hexdigest())
    cards = []
    for n, item in enumerate(raw, 1):
        card_id = f"XLC{n:02d}"
        cards.append({"card_id": card_id, "topic_id": TOPIC_ID, "phase": "exposure", "stance": item["stance"],
                      "is_accurate": item["is_accurate"], "text": item["text"],
                      "source_family_id": "F_XL_FORUM",
                      "source_url": f"https://forum.xinglan.synthetic.invalid/thread/{card_id.lower()}",
                      "transformation_type": "synthetic_closed_world", "is_demo": False,
                      "targeted_items": item["targets"]})
    return cards


def _block(round_no):
    parts = [f"[{eid} | {pub} {date}]\n{text}" for eid, r, pub, date, _f, _h, _p, text in EVIDENCE if r == round_no]
    return "\n\n".join(parts)


def _round_evidence(packet, round_no):
    ids = [e[0] for e in EVIDENCE if e[1] == round_no]
    return [deepcopy(e) for e in packet["evidence"] if e["evidence_id"] in ids]


def _visible(block, addition):
    return extractive_control._visible(block, addition)


def _feasible_window(packet, round_no):
    """枚举 I4 附加段整数长度，返回能被原句摘录在 ±10% 内配长、且双方 600–800 的最长连续区间。"""
    policy = extractive_control._POLICY
    evidence = _round_evidence(packet, round_no)
    fixed = len(_visible(_block(round_no), ""))
    sizes = set()
    pools = []
    for entry in evidence:
        spans = extractive_control._sentence_spans(entry["text"])
        pools.append([len(entry["text"][a:b]) + len(entry["evidence_id"]) + 2 for a, b in spans
                      if "？" not in entry["text"][a:b]])
    from itertools import product
    for combo in product(*pools):
        sizes.add(sum(combo))
    low, high, tol = policy["total_min_chars"], policy["total_max_chars"], policy["match_tolerance"]
    ok = []
    for length in range(1, high + 1):
        if not low <= fixed + length <= high:
            continue
        if any(abs(length - s) / min(length, s) <= tol and low <= fixed + s <= high
               and abs((fixed + length) - (fixed + s)) / min(fixed + length, fixed + s) <= tol for s in sizes):
            ok.append(length)
    _need(ok, f"round_{round_no}_no_feasible_window")
    best, run = [ok[0], ok[0]], [ok[0], ok[0]]
    for value in ok[1:]:
        run = [run[0], value] if value == run[1] + 1 else [value, value]
        if run[1] - run[0] > best[1] - best[0]:
            best = list(run)
    return {"round": round_no, "fixed_chars": fixed, "min": best[0], "max": best[1],
            "extract_sizes": sorted(sizes)}


def _i0_texts(cards, stance):
    """I0：同一讨论区同立场观点帖回放（无纠错、无新数字），按卡序确定性拼至 600–800。"""
    posts = [c["text"] for c in cards if c["stance"] == stance and c["is_accurate"]]
    head = "阅读材料\n同一讨论区里与你看法相近的帖子（摘编）：\n"
    tail = "\n\n" + RESPONSE_PROMPT
    out, start = {}, 0
    for round_no in INTERVENTION_ROUNDS:
        lines, n = [], start
        while True:
            line = f"{len(lines) + 1}．" + posts[n % len(posts)]
            if len(head + "\n".join(lines + [line]) + tail) > 690:
                break
            lines.append(line)
            n += 1
        text = head + "\n".join(lines) + tail
        _need(600 <= len(text) <= 800, "i0_dose_out_of_range")
        out[round_no] = {"visible_text": text, "char_count": len(text), "content_origin": "same_stance_forum_replay",
                         "visible_text_sha256": _sha_text(text), "post_indices": [i % len(posts) for i in range(start, n)]}
        start = n
    return out


def _persona(subject):
    side = AXIS["positive_label"] if STANCES[subject] > 0 else AXIS["negative_label"]
    return (f"你是一名普通的社交平台用户，正在参与关于“{TOPIC_TEXT}”的公共讨论。在“{AXIS['name']}”"
            f"这个问题上，你原本更倾向于“{side}”这一侧。请保持普通人的口吻，只依据你实际读到的内容思考，"
            "不编造没读过的材料，也不要假装自己是专家。" + CLOSED_WORLD_RULE)


def build_closed_world_materials() -> dict:
    """确定性生成封闭情境全部材料（无模型调用、无文件读写）。"""
    packet = _packet()
    questionnaire = _questionnaire()
    gold = _gold(questionnaire)
    cards = _card_pool()
    schedules, exposure_audit = {}, {}
    for n, subject in enumerate(SUBJECTS):
        stance = STANCES[subject]
        exposure = scheduling.schedule_cards(cards, stance, ENVIRONMENT, EXPOSURE_ROUNDS, BASE_SEED + 11 * n,
                                             max_repeats=2)
        retention = scheduling.schedule_cards(cards, stance, ENVIRONMENT, RETENTION_ROUNDS, BASE_SEED + 11 * n + 5,
                                              max_repeats=2)
        schedules[subject] = {"exposure": [[scheduling.public_card(c) for c in b] for b in exposure],
                              "retention": [[scheduling.public_card(c) for c in b] for b in retention]}
        audit = {}
        for phase, plan in (("exposure", exposure), ("retention", retention)):
            counts = {i[0]: 0 for i in ITEMS}
            for batch in plan:
                for card in batch:
                    for item in card["targeted_items"]:
                        counts[item] += 1
            audit[phase] = {"misleading_card_deliveries": sum(1 for b in plan for c in b if not c["is_accurate"]),
                            "same_stance_deliveries": sum(1 for b in plan for c in b if c["stance"] == stance),
                            "card_deliveries": sum(len(b) for b in plan),
                            "misleading_mentions_by_item": counts}
        exposure_audit[subject] = audit
    windows = {r: _feasible_window(packet, r) for r in INTERVENTION_ROUNDS}
    blocks = {r: _block(r) for r in INTERVENTION_ROUNDS}
    i0 = {subject: _i0_texts(cards, STANCES[subject]) for subject in SUBJECTS}
    item_design = [{"item_id": i, "partition": p, "statement": s, "truth": t,
                    "misled_by": {1: "pro_A_rumor", -1: "pro_B_rumor", None: None}[m]} for i, p, s, t, m in ITEMS]
    public = {"version": MATERIALS_VERSION, "packet": packet, "questionnaire": questionnaire,
              "cards": [scheduling.public_card(c) for c in cards], "blocks": blocks, "i0": i0,
              "personas": {s: _persona(s) for s in SUBJECTS}, "windows": windows}
    private = {"gold": gold, "card_design": [{k: c[k] for k in ("card_id", "stance", "is_accurate", "targeted_items")}
                                             for c in cards], "item_design": item_design}
    return {"packet": packet, "packet_sha256": canonical_hash(packet), "questionnaire": questionnaire,
            "gold": gold, "cards": cards, "schedules": schedules, "exposure_audit": exposure_audit,
            "windows": windows, "blocks": blocks, "i0": i0, "item_design": item_design,
            "personas": public["personas"], "axis": dict(AXIS), "topic": TOPIC_TEXT,
            "materials_public_sha256": canonical_hash(public),
            "materials_private_sha256": canonical_hash(private),
            "materials_sha256": canonical_hash({"public": public, "private": private}),
            "is_synthetic_closed_world": True}


# ------------------------------------------------------------------ I4 组织段与 I5 配对
ORGANIZER_SYSTEM = (
    "你是“破茧助手”的组织与反思模块。你会收到：同一位读者此前在讨论区发表的一段看法、针对这段看法的镜像诊断、"
    "四阶段整合与盲区分析的结构化输出，以及本轮阅读材料中将要展示的核查证据块（含证据编号）。"
    "请为本轮写一段“阅读说明”，帮助这位读者对照本轮证据检查自己此前的判断。要求："
    "一、只使用本轮证据块中出现的事实与数字，不得引入证据中没有的数字、机构或新事实；"
    "二、用中性、不指责的语气点出读者此前说法中与本轮证据不一致或缺少依据之处；"
    "三、至少写一句以问号结尾的反思提问，引导读者区分网传数字与核查来源；"
    "四、依据盲区分析提醒一到两个读者尚未考虑的方面，并说明结论的适用条件；"
    "五、不评判哪一种价值取向更好，只帮助读者把数字事实与价值取舍分开。"
    "只输出一个严格JSON对象，不要代码围栏或说明文字，格式为"
    "{\"sentences\":[{\"text\":\"一句中文\",\"citations\":[\"本轮证据编号\"]}]}。"
    "text必须是一句完整中文，不含任何英文字母、换行或Markdown，证据编号只写在citations数组里；"
    "citations只能取本轮证据编号且不能为空、不能重复。每句会被渲染为“text（编号）”，"
    "全部句子渲染后的总字数必须落在给定范围内。"
    "篇幅硬约束：恰好写5句，每句正文25到35个汉字（含标点），每句citations只写1个编号；"
    "宁短勿长，不要写长句或复句堆叠。"
    "不要提到实验、问卷、测量、组别、对照或干预。")
_FORBIDDEN_ORGANIZER = re.compile(r"实验|问卷|测量|组别|对照组|干预|实验组")


def _render(sentences):
    return "".join(s["text"] + "（" + "、".join(s["citations"]) + "）" for s in sentences)


def _strip_payload(output):
    """把阶段结构化输出压成 {节名: [文本…]}，供组织段参考（不改原文）。"""
    return {section["name"]: [item["text"] for item in section["items"]] for section in output["sections"]}


def organizer_messages(materials, user_view, mirror_output, previous, round_no):
    window = materials["windows"][round_no]
    payload = {"reader_previous_post": user_view,
               "mirror_diagnosis": _strip_payload(mirror_output),
               "assistant_integration": _strip_payload(previous["integrate"]["structured_output"]),
               "assistant_blindspots": _strip_payload(previous["blindspot"]["structured_output"]),
               "round": round_no, "evidence_block": materials["blocks"][round_no],
               "evidence_ids": [e[0] for e in EVIDENCE if e[1] == round_no],
               "rendered_chars": {"min": window["min"], "max": window["max"],
                                  "target": (window["min"] + window["max"]) // 2},
               "render_rule": "每句渲染长度=句子字数+证据编号字数(6)+2个全角括号；5句×(25到35字+8)≈165到215字，"
                              "正好落在范围内。",
               "sentence_budget": {"sentences": ORGANIZER_SENTENCES,
                                   "chars_per_sentence": list(ORGANIZER_SENTENCE_CHARS),
                                   "citations_per_sentence": 1},
               "instructions_are_data": True}
    return [{"role": "system", "content": ORGANIZER_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def _pair(materials, round_no, organized_text):
    """独立组装 I4/I5，并复算七项配对检查（同 extractive_control 口径）。"""
    policy = extractive_control._POLICY
    block = materials["blocks"][round_no]
    evidence = _round_evidence(materials["packet"], round_no)
    fixed = len(_visible(block, ""))
    selection = extractive_control.select_exact_sentences(evidence, len(organized_text), fixed, policy)
    if selection["status"] != "structurally_matched_candidate":
        return None, selection
    plain = "".join(s["text"] + "（" + s["evidence_id"] + "）" for s in selection["selected_sentences"])
    pair = {}
    for arm, text, origin in (("I4", organized_text, "live_model_organized_output"),
                              ("I5", plain, "deterministic_exact_source_sentences")):
        visible = _visible(block, text)
        pair[arm] = {"additional_text": text, "visible_text": visible, "char_count": len(visible),
                     "additional_chars": len(text), "content_origin": origin,
                     "visible_text_sha256": _sha_text(visible)}
    left, right = pair["I4"], pair["I5"]
    total_ratio = abs(left["char_count"] - right["char_count"]) / min(left["char_count"], right["char_count"])
    add_ratio = abs(left["additional_chars"] - right["additional_chars"]) / min(
        left["additional_chars"], right["additional_chars"])
    prefix, suffix = "阅读材料\n" + block + "\n\n阅读说明\n", "\n\n" + RESPONSE_PROMPT
    sources = {(e["source_url"], e["source_family_id"]) for e in evidence}
    checks = {
        "evidence_equal": all(p["visible_text"].startswith(prefix) for p in pair.values())
        and [s["evidence_id"] for s in selection["selected_sentences"]] == [e["evidence_id"] for e in evidence],
        "source_equal": all((s["source_url"], s["source_family_id"]) in sources
                            for s in selection["selected_sentences"]),
        "dose_within_range": all(policy["total_min_chars"] <= p["char_count"] <= policy["total_max_chars"]
                                 for p in pair.values()),
        "length_matched": total_ratio <= policy["match_tolerance"],
        "additional_length_matched": add_ratio <= policy["match_tolerance"],
        "response_equal": all(p["visible_text"].endswith(suffix) for p in pair.values()),
        "total_difference_ratio": round(total_ratio, 6), "additional_difference_ratio": round(add_ratio, 6)}
    checks["assembly_valid"] = all(checks[k] for k in extractive_control._CHECK_NAMES if k != "assembly_valid") \
        and all(p["visible_text"] == prefix + p["additional_text"] + suffix for p in pair.values()) \
        and not re.search(r"I[045]", left["visible_text"] + right["visible_text"])
    return {"pair": pair, "checks": checks,
            "i5_selected_sentence_indices": selection["selected_sentence_indices"],
            "i5_candidate_combination_count": selection["candidate_combination_count"]}, selection


def _organizer_validator(materials, round_no):
    """单轮组织段：形状、正文、引用、反思问句、渲染窗口与 I5 可配长逐项校验（只诊断不修复）。"""
    ids = [e[0] for e in EVIDENCE if e[1] == round_no]

    def check(raw):
        try:
            data = json.loads(raw)
        except ValueError:
            return None, "closed_world_pilot:organizer_not_json", {"json_parse_failed": True}
        sentences = data.get("sentences") if type(data) is dict else None
        if not (type(data) is dict and set(data) == {"sentences"} and type(sentences) is list
                and 2 <= len(sentences) <= 12):
            return None, "closed_world_pilot:organizer_shape_invalid", {"shape_invalid": True}
        issues = {}
        for s in sentences:
            if type(s) is not dict or set(s) != {"text", "citations"} or type(s["text"]) is not str \
                    or type(s["citations"]) is not list:
                issues["sentence_shape_invalid"] = True
                continue
            text = s["text"]
            if not 6 <= len(text) <= 160 or "\n" in text or "\r" in text:
                issues["sentence_length_or_newline"] = True
            letters = re.findall(r"[A-Za-z]+", text)
            if letters:
                issues.setdefault("latin_letters", []).extend(letters[:4])
            if _FORBIDDEN_ORGANIZER.search(text):
                issues["forbidden_terms"] = True
            if not s["citations"] or len(set(s["citations"])) != len(s["citations"]) \
                    or any(c not in ids for c in s["citations"]):
                issues["citations_invalid"] = {"allowed": ids, "given": s["citations"][:5]}
        if not issues and not any(("？" in s["text"] or "?" in s["text"]) for s in sentences):
            issues["missing_reflection_question"] = True
        if not issues:
            rendered = _render(sentences)
            window = materials["windows"][round_no]
            if not window["min"] <= len(rendered) <= window["max"]:
                issues["rendered_length"] = {"length": len(rendered), "min": window["min"], "max": window["max"]}
            else:
                paired, _ = _pair(materials, round_no, rendered)
                if paired is None or not paired["checks"]["assembly_valid"]:
                    issues["no_matched_extract"] = True
                else:
                    return {round_no: {"sentences": sentences, "rendered": rendered, **paired}}, None, {}
        return None, "closed_world_pilot:organizer_rejected", {f"round_{round_no}": issues}
    return check


def _organizer_feedback(code, details):
    lines = ["上一次输出未通过本地严格校验，请仅修正下列问题后重新完整输出本轮；只输出一个JSON对象。"]
    details = details or {}
    if details.get("json_parse_failed") or details.get("shape_invalid"):
        lines.append("输出必须是一个完整闭合的 {\"sentences\":[{\"text\":\"…\",\"citations\":[\"…\"]}]}，"
                     "恰好5句，不要代码围栏或任何其他文字。")
    for key, issues in details.items():
        if not key.startswith("round_") or type(issues) is not dict:
            continue
        r = key.split("_")[1]
        if "rendered_length" in issues:
            info = issues["rendered_length"]
            target = (info["min"] + info["max"]) // 2
            direction = "删减" if info["length"] > info["max"] else "补充"
            lines.append(f"第{r}轮渲染后共{info['length']}字，必须在{info['min']}到{info['max']}字之间，"
                         f"请{direction}到约{target}字（编号与括号也计入字数）：改为恰好5句、每句正文25到35字、"
                         f"每句只引1个编号。")
        if "latin_letters" in issues:
            lines.append(f"第{r}轮句子正文含英文字母（{'、'.join(issues['latin_letters'][:4])}），"
                         "证据编号只能写在citations里，正文改为中文表述。")
        if "citations_invalid" in issues:
            lines.append(f"第{r}轮citations只能取{issues['citations_invalid']['allowed']}中的编号，且不能为空或重复。")
        if issues.get("missing_reflection_question"):
            lines.append("5句中至少有一句必须是以“？”结尾的反思提问（例如把一句陈述改写为向读者提问）。")
        if issues.get("forbidden_terms"):
            lines.append(f"第{r}轮不要出现实验、问卷、测量、组别、对照或干预等词。")
        if issues.get("no_matched_extract"):
            lines.append(f"第{r}轮总字数请向范围中部靠拢。")
        if issues.get("sentences_invalid") or issues.get("sentence_shape_invalid") \
                or issues.get("sentence_length_or_newline"):
            lines.append(f"第{r}轮应有2到12句，每句仅含text与citations，text为6到160字、不换行。")
    return {"rejected_error_code": code, "diagnostics": details, "instruction": "".join(lines)}


class _Runner(lmp._Runner):
    """与步骤40/41 相同的三次重试与留痕；组织段使用专用诊断反馈。"""

    def call(self, node_id, kind, messages, validator, *, max_tokens, meta, envelope=False):
        node = {"node_id": node_id, "kind": kind, **meta, "attempts": [], "accepted": False,
                "accepted_attempt": None, "accepted_raw_text": None, "accepted_response_sha256": None}
        self.nodes.append(node)
        feedback = None
        settings = dict(self.settings, max_tokens=max_tokens)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wire = deepcopy(messages)
            if feedback is not None:
                wire[-1] = {"role": wire[-1]["role"], "content": wire[-1]["content"]
                            + "\n\n【上一次输出的校验反馈】\n" + json.dumps(feedback, ensure_ascii=False)}
            request = {"request_id": f"{self.run_id}:{node_id}:{attempt}", "stage": kind,
                       "is_demo": False, "model_settings": settings, "messages": wire}
            request_ref = self.store.capture(request)
            self.store.append("micro_request_prepared", task_id=request["request_id"],
                              input_ref=request_ref["ref"], input_sha256=request_ref["sha256"],
                              payload={"node_id": node_id, "attempt": attempt, "feedback": feedback})
            row = {"attempt": attempt, "request_id": request["request_id"], "request_sha256": request_ref["sha256"]}
            node["attempts"].append(row)
            try:
                response = self.transport.complete(request)
            except (MiMoError, BudgetError) as exc:
                row.update(status="transport_error", error=getattr(exc, "code", type(exc).__name__))
                self.store.append("micro_attempt_aborted", task_id=request["request_id"],
                                  payload={"node_id": node_id, "attempt": attempt, "error": row["error"]})
                raise
            response_ref = self.store.capture(response)
            row["response_sha256"] = response_ref["sha256"]
            row["usage"] = response["usage"]
            value, error, diag = validator(response if envelope else response["raw_text"])
            if error is None:
                row.update(status="accepted")
                event = self.store.append("micro_attempt_accepted", task_id=request["request_id"],
                                          input_ref=request_ref["ref"], input_sha256=request_ref["sha256"],
                                          output_ref=response_ref["ref"], output_sha256=response_ref["sha256"],
                                          payload={"node_id": node_id, "attempt": attempt})
                row["event_id"] = event["event_id"]
                node.update(accepted=True, accepted_attempt=attempt, accepted_raw_text=response["raw_text"],
                            accepted_response_sha256=response_ref["sha256"], accepted_event_id=event["event_id"])
                return value, node
            row.update(status="rejected", error=error, diagnostics=diag)
            event = self.store.append("micro_attempt_rejected", task_id=request["request_id"],
                                      input_ref=request_ref["ref"], input_sha256=request_ref["sha256"],
                                      output_ref=response_ref["ref"], output_sha256=response_ref["sha256"],
                                      payload={"node_id": node_id, "attempt": attempt, "error": error,
                                               "diagnostics": diag})
            row["event_id"] = event["event_id"]
            feedback = (_organizer_feedback if kind == "i4_organizer" else lmp._feedback)(error, diag)
        raise ValueError(f"closed_world_pilot:node_failed:{node_id}")


# ------------------------------------------------------------------ 四阶段复用（只依赖冻结证据包）
# 第二次运行（closed_world_synth01_20261114T042318Z_3a69aa08）四阶段三次均在 blindspot 原样复述输出契约而失败；
# 首跑（…041348Z_6b4a4043）第 2 次四阶段任务已通过冻结校验。四阶段输入只有同一冻结合成证据包（与受试无关），
# 故最终运行复用该已校验输出：来源事件链、包哈希、模型设置与逐阶段 blob 均在运行时与离线核验中重验。
FOUR_STAGE_SOURCE = {"run_id": "closed_world_synth01_20261114T041348Z_6b4a4043",
                     "task_id": "closed_world_synth01_20261114T041348Z_6b4a4043:assistant2"}
_FOUR = ("search", "label", "blindspot", "integrate")


def load_four_stage_source(materials, source=None, *, repo_root=None, settings=None):
    """从指定运行的事件链重建已校验四阶段输出；任何一项不一致即拒绝。"""
    from .contracts import parse_stage_output
    source = source or FOUR_STAGE_SOURCE
    repo = Path(repo_root).resolve() if repo_root is not None else ROOT
    run_dir = (repo / RUNS_RELATIVE / source["run_id"]).resolve(strict=True)
    chain = verify_events(run_dir)
    _need(chain.get("chain_valid") is True, "four_stage_source_chain_invalid")
    rows = [_parse_json(line.encode("utf-8")) for line in
            (run_dir / "events/events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    task = [r for r in rows if r.get("task_id") == source["task_id"]]
    kinds = [r["event_type"] for r in task]
    _need("assistant_task_completed" in kinds and "assistant_task_failed" not in kinds, "four_stage_source_not_completed")
    preflight = next(r for r in task if r["event_type"] == "assistant_preflight_passed")
    packet = _verify_blob(run_dir, preflight["payload"]["packet"])
    _need(packet == materials["packet"], "four_stage_source_packet_mismatch")
    if settings is not None:
        _need(preflight["payload"]["model_settings"] == dict(settings, max_tokens=4096), "four_stage_source_settings_mismatch")
    previous, provenance = {}, {}
    for stage in _FOUR:
        event = next(r for r in task if r["event_type"] == "assistant_stage_validated" and r.get("phase") == stage)
        blob = _verify_blob(run_dir, event["payload"]["response"])
        parsed = parse_stage_output(blob["raw_text"], stage, packet)
        _need(parsed["output"] == event["payload"]["structured_output"], "four_stage_source_output_mismatch")
        previous[stage] = {"raw_text": blob["raw_text"], "structured_output": parsed["output"]}
        provenance[stage] = {"event_id": event["event_id"], "response_sha256": event["payload"]["response"]["sha256"]}
    return {"previous": previous,
            "provenance": {"run_id": source["run_id"], "task_id": source["task_id"],
                           "preflight_event_id": preflight["event_id"], "packet_sha256": canonical_hash(packet),
                           "model_settings": preflight["payload"]["model_settings"], "stages": provenance,
                           "source_chain": chain, "reason": "第二次运行四阶段三次在 blindspot 复述输出契约失败；"
                           "四阶段只依赖冻结合成证据包，复用首跑已通过冻结校验的输出。"}}


# ------------------------------------------------------------------ 运行
MEASURE_FORMAT_NOTE = (lmp.MEASURE_FORMAT_NOTE + "对星澜市具体数据的判断：尚未读到对应数据的题，p_true填0.45到0.55；"
                       "读到过相关数据或核查报告的题，以你读到的最新、最可信的核查证据为准。")


def run_closed_world_live_pilot(*, execute=False, config=None, root=None, runs_root=None,
                                manifest_path=None, four_stage_source="default") -> dict:
    """真实执行封闭情境完整链路；未授权或未就绪时 0 调用返回 blocked。"""
    repo = Path(root).resolve() if root is not None else ROOT
    if config is None:
        config = _load_config(repo)
    status = mimo_status(config, repo)
    base = {"schema_version": SCHEMA_VERSION, "contract": CONTRACT, "topic_id": TOPIC_ID, "executed": False,
            "status": "blocked", "blockers": [], "actual_model_calls": 0, "cost_cny": "0.00000000",
            "billing_before": status.get("billing"), "is_synthetic_closed_world": True}
    if status["blockers"]:
        return {**base, "blockers": list(status["blockers"])}
    if execute is not True:
        return {**base, "blockers": ["execute_required"]}
    materials = build_closed_world_materials()
    settings = lmp._settings(config)
    ledger = _ledger(config, repo)
    billing_before = ledger.status()
    parent = Path(runs_root) if runs_root is not None else repo / RUNS_RELATIVE
    parent.mkdir(parents=True, exist_ok=True)
    run_id = RUN_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    run_dir = parent / run_id
    run_dir.mkdir()
    store = EventStore(run_dir, {"contract": CONTRACT, "model_settings": settings, "topic_id": TOPIC_ID,
                                 "block_id": BLOCK_ID, "environment": ENVIRONMENT, "subjects": list(SUBJECTS),
                                 "arms": list(ARMS), "flatten_rule": lmp.FLATTEN_RULE,
                                 "registry_seed": REGISTRY_SEED, "thinking": "disabled",
                                 "is_synthetic_closed_world": True, "protocol_basis": PROTOCOL_BASIS,
                                 "materials_sha256": materials["materials_sha256"]},
                       materials["packet_sha256"], False, run_id)
    transport = MiMoTransport(config["models"]["assistant"], store, ledger)
    runner = _Runner(store, transport, run_id, settings)
    store.append("closed_world_pilot_started", payload={"billing_before": billing_before,
                 "scope": "closed_world_synthetic_n2_x3_not_formal",
                 "materials_sha256": materials["materials_sha256"]})
    record = {"run_id": run_id, "run_directory": run_dir.as_posix(), "status": "running", "error": None}
    state = {}
    try:
        reuse = None
        if four_stage_source is not None:
            reuse = load_four_stage_source(materials, None if four_stage_source == "default" else four_stage_source,
                                           repo_root=repo, settings=settings)
            store.append("closed_world_four_stage_reused", payload=reuse["provenance"])
        state = _execute(materials, runner, store, config, run_id, settings, transport, reuse)
        record["status"] = "completed"
    except (MiMoError, BudgetError, ValueError) as exc:
        record.update(status="incomplete", error={"code": getattr(exc, "code", None) or str(exc)[:200]})
        store.append("closed_world_pilot_failed", payload=record["error"])
    billing_after = ledger.status()
    cost = "%.8f" % (float(billing_after["charged_cny"]) - float(billing_before["charged_cny"]))
    summary = {"schema_version": SCHEMA_VERSION, "contract": CONTRACT, "topic_id": TOPIC_ID,
               "subjects": list(SUBJECTS), "prompt_revision": lmp.PROMPT_REVISION,
               "feedback_revision": lmp.FEEDBACK_REVISION, "organizer_revision": ORGANIZER_REVISION, **record,
               "actual_model_calls": transport.actual_model_calls, "nodes": runner.nodes,
               "billing_before": billing_before, "billing_after": billing_after, "cost_cny": cost,
               "model_settings": settings, "flatten_rule": lmp.FLATTEN_RULE, "completed_at_utc": utc_now(),
               "materials_sha256": materials["materials_sha256"], **state}
    store.append("closed_world_pilot_completed" if record["status"] == "completed" else "closed_world_pilot_stopped",
                 payload={"status": record["status"], "actual_model_calls": transport.actual_model_calls,
                          "cost_cny": cost})
    summary["verification"] = verify_events(run_dir)
    write_json_new(run_dir / "summary.json", summary)
    result = {**base, "executed": True, "status": record["status"], "run_id": run_id,
              "run_directory": run_dir.as_posix(), "actual_model_calls": transport.actual_model_calls,
              "cost_cny": cost, "billing_after": billing_after, "error": record["error"]}
    if record["status"] == "completed":
        result["manifest"] = build_closed_world_manifest(run_dir, repo_root=repo, manifest_path=manifest_path)
    return result


def _execute(materials, runner, store, config, run_id, settings, transport, reuse=None):
    q, gold, packet = materials["questionnaire"], materials["gold"], materials["packet"]
    personas = materials["personas"]
    histories = {s: [] for s in SUBJECTS}
    exposure_events, speeches, measurements = [], [], {}

    def measure(world_state, subject, arm, timepoint, world_id):
        ctx = measurement.prepare_subject_measurement_context(world_state, subject, q)
        _need(ctx["timepoint"] == timepoint, "context_timepoint_mismatch")
        messages = ctx["messages"][:-1] + [{"role": "system", "content": lmp.MEASURE_GUIDE}, ctx["messages"][-1]]
        node_id = f"measure_{subject}_{arm or 'P'}_{timepoint}"
        record, _node = runner.call(node_id, "subject_measurement", lmp._flatten(messages, MEASURE_FORMAT_NOTE),
                                    lmp._measure_validator(materials, ctx["visible_card_ids"], timepoint),
                                    max_tokens=3072, meta={"subject": subject, "arm": arm, "timepoint": timepoint,
                                                           "world_id": world_id,
                                                           "visible_card_ids": ctx["visible_card_ids"],
                                                           "context_sha256": canonical_hash(ctx["messages"])})
        measurements[(subject, arm, timepoint)] = {"record": record, "world_id": world_id}

    for subject in SUBJECTS:   # T0：零曝光未知基线
        world = lmp._world(PARENT_WORLD, SUBJECTS, "T0", 0, 0, histories, exposure_events, speeches, personas[subject])
        measure(world, subject, None, "T0", PARENT_WORLD)

    for round_no in range(1, EXPOSURE_ROUNDS + 1):   # 环境 D：同质 4/1/1 + 每轮 2 张误导卡
        for subject in SUBJECTS:
            cards = materials["schedules"][subject]["exposure"][round_no - 1]
            exposure_events.append({"event_id": f"EXP_{subject}_R{round_no:02d}", "event_type": "exposure_delivered",
                                    "agent_id": subject, "status": "delivered", "sent_to_model": True,
                                    "cards": deepcopy(cards)})
            histories[subject].append({"role": "user", "content": lmp._cards_message(f"第{round_no}轮信息流", cards)})
    store.append("micro_exposure_delivered", payload={
        "rounds": EXPOSURE_ROUNDS, "card_ids": {s: [[c["card_id"] for c in b]
                                                    for b in materials["schedules"][s]["exposure"]] for s in SUBJECTS},
        "misleading_audit": materials["exposure_audit"]})
    task = lmp.SPEECH_TASK.format(low=lmp.SPEECH_RANGE[0], high=lmp.SPEECH_RANGE[1], target=lmp.SPEECH_TARGET)
    for n, subject in enumerate(SUBJECTS, 1):
        messages = [{"role": "system", "content": personas[subject]}] + histories[subject] + [
            {"role": "user", "content": task}]

        def routine_check(text, subject=subject, n=n):
            row = _speech_row(subject, n, text)
            probe = lmp._world(PARENT_WORLD, SUBJECTS, "T1", 8, 900, histories, exposure_events, [row],
                               personas[subject])
            try:
                measurement.extract_t1_routine_speech_view(probe, subject, topic_id=TOPIC_ID)
            except ValueError as exc:
                return str(exc), {"scope": "常规发言含编号、英文或测量措辞"}
            return None

        speech, _node = runner.call(f"speech_{subject}_T1", "routine_speech", lmp._flatten(messages),
                                    lmp._plain_validator(*lmp.SPEECH_RANGE, extra=routine_check), max_tokens=1024,
                                    meta={"subject": subject, "arm": None, "timepoint": "T1",
                                          "world_id": PARENT_WORLD})
        speeches.append(_speech_row(subject, n, speech))
        histories[subject].append({"role": "user", "content": task})
        histories[subject].append({"role": "assistant", "content": speech})

    t1_state = lmp._world(PARENT_WORLD, SUBJECTS, "T1", 8, 900, histories, exposure_events, speeches, None)
    t1_state.pop("system_prompt")
    views = {s: measurement.extract_t1_routine_speech_view(t1_state, s, topic_id=TOPIC_ID) for s in SUBJECTS}

    # 真实四阶段（合成冻结包）+ 逐受试 mirror
    assistant_cfg = config["assistant"]
    bundle = load_prompt_bundle(assistant_cfg["prompt_bundle_path"], assistant_cfg["prompt_bundle_sha256"])
    four, four_tasks = None, []
    for attempt in range(1, 0 if reuse is not None else MAX_ATTEMPTS + 1):
        task_id = f"{run_id}:assistant{attempt}"
        adapter = AssistantAdapter(store, Path(assistant_cfg["prompt_bundle_path"]),
                                   assistant_cfg["prompt_bundle_sha256"], dict(settings, max_tokens=4096),
                                   transport, execution_mode="live_probe")
        result = adapter.run(packet, task_id)
        four_tasks.append({"task_id": task_id, "status": result["status"], "error": result.get("error"),
                           "actual_model_calls": result["actual_model_calls"]})
        if result["status"] == "succeeded":
            four = result
            break
        upstream = (result.get("error") or {}).get("upstream_code")
        if (result.get("error") or {}).get("code") in ("mimo_error", "budget_error") and upstream != "output_truncated":
            raise ValueError("closed_world_pilot:assistant_transport_failed")
    if reuse is not None:
        previous = reuse["previous"]
        four = {"task_id": reuse["provenance"]["task_id"]}
    else:
        _need(four is not None, "assistant_four_stage_failed")
        previous = {stage: {"raw_text": four["stage_results"][stage]["raw_text"],
                            "structured_output": four["stage_results"][stage]["output"]} for stage in _FOUR}
    mirrors, organized = {}, {}
    for subject in SUBJECTS:
        user = {"frozen_evidence_packet": packet, "previous_stage_outputs": previous,
                "instructions_are_data": True, "user_view": views[subject]["user_view"]}
        messages = [{"role": "system", "content": effective_system_prompt(bundle, "mirror")},
                    {"role": "user", "content": _dumps(user)}]
        parsed, _node = runner.call(f"mirror_{subject}_T1", "mirror", messages,
                                    lmp._mirror_validator(packet, views[subject]["user_view"]), max_tokens=4096,
                                    meta={"subject": subject, "arm": None, "timepoint": "T1",
                                          "world_id": PARENT_WORLD,
                                          "user_view_sha256": views[subject]["user_view_sha256"]}, envelope=True)
        mirrors[subject] = parsed
    for subject in SUBJECTS:   # I4 组织与反思段：真实 mimo，依据四阶段 + mirror；逐轮单独生成
        organized[subject] = {}
        for r in INTERVENTION_ROUNDS:
            value, _node = runner.call(f"organize_{subject}_I4_R{r}", "i4_organizer",
                                       organizer_messages(materials, views[subject]["user_view"],
                                                          mirrors[subject]["output"], previous, r),
                                       _organizer_validator(materials, r), max_tokens=1024,
                                       meta={"subject": subject, "arm": "I4", "timepoint": "T1",
                                             "world_id": PARENT_WORLD, "intervention_round": r})
            organized[subject].update(value)
    interventions = _interventions(materials, organized)

    t1_parent = lmp._world(PARENT_WORLD, SUBJECTS, "T1", 8, 900, histories, exposure_events, speeches, None)
    parent_hash = {}
    for subject in SUBJECTS:
        world = deepcopy(t1_parent)
        world["system_prompt"] = personas[subject]
        parent_hash[subject] = lmp._state_core(world)
        measure(world, subject, None, "T1", PARENT_WORLD)

    branches = {}
    guide = lmp.RESPONSE_GUIDE.format(low=lmp.RESPONSE_RANGE[0], high=lmp.RESPONSE_RANGE[1],
                                      target=lmp.RESPONSE_TARGET)
    for subject in SUBJECTS:
        for arm in ARMS:
            world_id = f"{PARENT_WORLD}_{arm}"
            world = deepcopy(t1_parent)
            world["system_prompt"] = personas[subject]
            pre_hash = lmp._state_core(world)
            _need(pre_hash == parent_hash[subject], "branch_pre_treatment_hash_mismatch")
            world["world_id"] = world_id
            hist = world["messages"][subject]
            responses = []
            for round_no in INTERVENTION_ROUNDS:
                item = interventions[subject][arm][round_no]
                text = item["visible_text"]
                messages = ([{"role": "system", "content": personas[subject]}] + hist
                            + [{"role": "user", "content": text + "\n\n" + guide}])
                reply, _node = runner.call(f"respond_{subject}_{arm}_R{round_no}", "intervention_response",
                                           lmp._flatten(messages), lmp._plain_validator(*lmp.RESPONSE_RANGE),
                                           max_tokens=1024,
                                           meta={"subject": subject, "arm": arm, "timepoint": "T2",
                                                 "world_id": world_id, "intervention_round": round_no,
                                                 "intervention_sha256": item["visible_text_sha256"]})
                hist.append({"role": "user", "content": lmp._single_line(text + "\n\n" + guide)})
                hist.append({"role": "assistant", "content": reply})
                responses.append(reply)
            world["timepoint"] = "T2"
            measure(world, subject, arm, "T2", world_id)
            for round_no in range(1, RETENTION_ROUNDS + 1):
                cards = materials["schedules"][subject]["retention"][round_no - 1]
                world["exposure_history"].append({
                    "event_id": f"RET_{subject}_{arm}_R{round_no:02d}", "event_type": "exposure_delivered",
                    "agent_id": subject, "status": "delivered", "sent_to_model": True, "cards": deepcopy(cards)})
                hist.append({"role": "user", "content": lmp._cards_message(f"保持期第{round_no}轮信息流", cards)})
            world["timepoint"] = "T3"
            measure(world, subject, arm, "T3", world_id)
            branches[(subject, arm)] = {"world_id": world_id, "pre_treatment_core_sha256": pre_hash,
                                        "responses": responses, "final_state_sha256": canonical_hash(world)}

    entries = [{"parsed_measurement": item["record"], "gold_bundle": gold, "world_id": item["world_id"],
                "agent_id": subject, "environment": ENVIRONMENT, "arm": arm, "timepoint": timepoint,
                "block_id": BLOCK_ID} for (subject, arm, timepoint), item in measurements.items()]
    registry = measurement.create_blind_sample_registry(entries, registry_seed=REGISTRY_SEED)
    evaluations = []
    for blind_packet in registry["public_blind_packets"]:
        sid = blind_packet["blind_sample_id"]
        messages = [{"role": "system", "content": lmp.BLIND_SYSTEM},
                    {"role": "user", "content": json.dumps(blind_packet, ensure_ascii=False)}]
        evaluation, _node = runner.call(f"blind_{sid}", "blind_evaluation", messages,
                                        lmp._blind_validator(blind_packet), max_tokens=2048,
                                        meta={"blind_sample_id": sid, "subject": None, "arm": None,
                                              "timepoint": None, "world_id": None})
        evaluations.append(evaluation)
    by_id = {sid: registry["source_entries"][bound["source_entry_index"]]["parsed_measurement"]
             for sid, bound in registry["private_mapping"].items()}
    panel = measurement.reconcile_blind_evaluations(registry, evaluations, by_id, gold,
                                                    expected_registry_sha256=registry["registry_sha256"])
    write_json_new(Path(store.root) / "controller_private_registry.json", registry)
    return {
        "four_stage_tasks": four_tasks, "four_stage_task_id": four["task_id"],
        "four_stage_reused_from": None if reuse is None else reuse["provenance"],
        "four_stage_outputs": {k: v["structured_output"] for k, v in previous.items()},
        "user_views": {s: {k: views[s][k] for k in ("user_view", "user_view_sha256", "speech_ids")} for s in SUBJECTS},
        "mirrors": {s: mirrors[s]["output"] for s in SUBJECTS},
        "mirror_citation_audit": {s: mirrors[s]["citation_audit"] for s in SUBJECTS},
        "interventions": _intervention_record(interventions),
        "parent_t1_core_sha256": parent_hash,
        "branches": [{"subject": s, "arm": a, **v} for (s, a), v in branches.items()],
        "registry_sha256": registry["registry_sha256"], "registry_seed": REGISTRY_SEED,
        "panel": panel, "panel_sha256": panel["panel_sha256"]}


def _speech_row(subject, n, text):
    return {"speech_id": f"SP_LIVE_{subject}", "event_id": f"RS_LIVE_{subject}", "world_id": PARENT_WORLD,
            "agent_id": subject, "topic_id": TOPIC_ID, "phase": "exposure", "round": 8,
            "logical_seq": 800 + n, "event_type": "routine_speech_generated", "role": "assistant",
            "status": "completed", "generated": True, "text": text, "is_demo": False}


def _interventions(materials, organized):
    """按受试组装三臂两轮可见文本：I0 同质回放；I4 真实组织段；I5 与 I4 逐轮配长的原句摘录。"""
    out = {}
    for subject in SUBJECTS:
        out[subject] = {"I0": {}, "I4": {}, "I5": {}, "pair_checks": {}}
        for r in INTERVENTION_ROUNDS:
            item = materials["i0"][subject][r]
            out[subject]["I0"][r] = {k: item[k] for k in ("visible_text", "char_count", "content_origin",
                                                          "visible_text_sha256")}
            paired = organized[subject][r]
            for arm in ("I4", "I5"):
                p = paired["pair"][arm]
                out[subject][arm][r] = {k: p[k] for k in ("visible_text", "char_count", "additional_chars",
                                                          "content_origin", "visible_text_sha256")}
            out[subject]["pair_checks"][r] = dict(paired["checks"],
                                                  i5_selected_sentence_indices=paired["i5_selected_sentence_indices"])
    return out


def _intervention_record(interventions):
    return {s: {arm: {str(r): v for r, v in rounds.items()} for arm, rounds in data.items()}
            for s, data in interventions.items()}


# ------------------------------------------------------------------ 清单
MISLED_ITEMS = tuple(i[0] for i in ITEMS if i[4] is not None and i[1] == "anchor")
UNMISLED_ANCHORS = tuple(i[0] for i in ITEMS if i[4] is None and i[1] == "anchor")
_TRUTH = {i[0]: i[3] for i in ITEMS}


def _probs(raw_text):
    return {a["item_id"]: a["p_true"] for a in json.loads(raw_text)["answers"]}


def _item_stats(probs, items):
    vals = [(probs.get(i), _TRUTH[i]) for i in items]
    answered = [(p, t) for p, t in vals if p is not None]
    if not answered:
        return {"n": 0, "correct": 0, "mean_abs_error": None, "brier": None}
    return {"n": len(answered), "correct": sum((p >= 0.5) == bool(t) for p, t in answered),
            "mean_abs_error": round(sum(abs(p - t) for p, t in answered) / len(answered), 6),
            "brier": round(sum((p - t) ** 2 for p, t in answered) / len(answered), 6)}


def _item_level(nodes):
    rows = []
    for node in nodes:
        if node["kind"] != "subject_measurement" or not node["accepted"]:
            continue
        probs = _probs(node["accepted_raw_text"])
        rows.append({"subject": node["subject"], "arm": node["arm"], "timepoint": node["timepoint"],
                     "p_true": {i[0]: probs.get(i[0]) for i in ITEMS},
                     "misled_anchor_stats": _item_stats(probs, MISLED_ITEMS),
                     "unmisled_anchor_stats": _item_stats(probs, UNMISLED_ANCHORS),
                     "transfer_stats": _item_stats(probs, [i[0] for i in ITEMS if i[1] == "transfer"]),
                     "t0_band_0_45_0_55_count": sum(0.45 <= probs.get(i[0], -1) <= 0.55 for i in ITEMS)})
    return rows


def _descriptive(comparison, item_level):
    """按臂×时点对 2 名受试取均值（描述性，不做推断）。"""
    out = []
    for arm in ARMS:
        row = {"arm": arm}
        for tp in ("T0", "T1", "T2", "T3"):
            cells = [r for r in comparison if r["arm"] == arm and r["timepoint"] == tp]
            for metric in ("BS", "Acc", "S", "O", "U"):
                row[f"{metric}_{tp}_mean"] = lmp._mean([c[metric] for c in cells])
            items = [r for r in item_level if r["timepoint"] == tp and (r["arm"] == arm or tp in ("T0", "T1"))]
            row[f"misled_correct_{tp}_mean"] = lmp._mean([r["misled_anchor_stats"]["correct"] for r in items])
        row["delta_BS_T1_T3_mean"] = (None if row["BS_T3_mean"] is None or row["BS_T1_mean"] is None
                                      else row["BS_T3_mean"] - row["BS_T1_mean"])
        out.append(row)
    return out


def _superseded_runs(repo, rows, current_run_id):
    out = []
    for path in sorted((Path(repo) / RUNS_RELATIVE).glob(RUN_PREFIX + "*/aborted.json")):
        info = _parse_json(path.read_bytes())
        if info["run_id"] == current_run_id:
            continue
        totals = lmp._ledger_totals(rows, info["run_id"])
        uncertain = [r for r in rows if r.get("type") == "uncertain"
                     and str(r.get("request_id", "")).startswith(info["run_id"] + ":")]
        out.append({"run_id": info["run_id"], "status": info["status"], "reason": info["reason"],
                    "settle_count": totals["settle_count"], "charged_cny": totals["charged_cny"],
                    "usage": totals["usage"],
                    "uncertain_requests": [{"request_id": r["request_id"], "reason": r["reason"]} for r in uncertain],
                    "aborted_record_sha256": canonical_hash(info)})
    return out


def build_closed_world_manifest(run_directory, *, repo_root=None, manifest_path=None) -> dict:
    """从运行目录 summary 汇总可核验清单（合成情境：题目真值与误导设计随清单公开）。"""
    repo = Path(repo_root).resolve() if repo_root is not None else ROOT
    run_dir = Path(run_directory).resolve(strict=True)
    summary = _parse_json((run_dir / "summary.json").read_bytes())
    _need(summary["status"] == "completed" and summary["run_id"].startswith(RUN_PREFIX), "run_not_completed")
    materials = build_closed_world_materials()
    _need(summary["materials_sha256"] == materials["materials_sha256"], "materials_changed_since_run")
    rows = lmp._ledger_rows(repo)
    ledger = lmp._ledger_totals(rows, summary["run_id"])
    superseded = _superseded_runs(repo, rows, summary["run_id"])
    panel = summary["panel"]
    nodes = []
    for node in summary["nodes"]:
        nodes.append({k: node.get(k) for k in (
            "node_id", "kind", "subject", "arm", "timepoint", "world_id", "blind_sample_id", "accepted",
            "accepted_attempt", "accepted_response_sha256", "accepted_event_id", "visible_card_ids",
            "intervention_round", "intervention_sha256")}
            | {"attempts": [{k: a.get(k) for k in ("attempt", "request_id", "status", "error", "response_sha256",
                                                    "usage")} for a in node["attempts"]],
               "accepted_text": node["accepted_raw_text"] if node["kind"] in (
                   "routine_speech", "intervention_response", "i4_organizer") else None})
    measured = {}
    for node in summary["nodes"]:
        if node["kind"] == "subject_measurement" and node["accepted"]:
            output = json.loads(node["accepted_raw_text"])
            measured[node["node_id"]] = {"opposing_view": output["opposing_view"],
                                         "evidence_notes": output["evidence_notes"],
                                         "brief_reasons": {a["item_id"]: a["brief_reason"] for a in output["answers"]}}
    comparison = lmp._table(panel["panel_records"], SUBJECTS)
    item_level = _item_level(summary["nodes"])
    rejected = sum(1 for n in summary["nodes"] for a in n["attempts"] if a["status"] == "rejected")
    four_calls = sum(a.get("actual_model_calls", 0) for a in summary["four_stage_tasks"])
    manifest = {
        "schema_version": SCHEMA_VERSION, "contract": CONTRACT, "created_at_utc": utc_now(),
        "run_id": summary["run_id"], "run_directory": run_dir.as_posix(),
        "closed_world": {"is_synthetic_closed_world": True, "protocol_basis": PROTOCOL_BASIS,
                         "protocol_note": PROTOCOL_NOTE, "topic_id": TOPIC_ID, "topic_label": TOPIC_LABEL,
                         "topic_text": TOPIC_TEXT, "stance_axis": AXIS, "closed_world_rule": CLOSED_WORLD_RULE,
                         "materials_version": MATERIALS_VERSION, "materials_sha256": materials["materials_sha256"],
                         "materials_public_sha256": materials["materials_public_sha256"],
                         "packet_sha256": materials["packet_sha256"],
                         "source_url_policy": "全部来源为 https://*.xinglan.synthetic.invalid/（RFC 2606 保留域，永不解析）",
                         "misinformation_card_policy": "误导卡仅为讨论区网帖口径，不伪造权威机构认可",
                         "item_design": materials["item_design"], "misled_anchor_items": list(MISLED_ITEMS),
                         "unmisled_anchor_items": list(UNMISLED_ANCHORS),
                         "exposure_audit": materials["exposure_audit"],
                         "evidence": [{"evidence_id": e[0], "round": e[1], "publisher_short": e[2], "date": e[3],
                                       "text": e[7]} for e in EVIDENCE],
                         "organizer_windows": {str(r): {k: w[k] for k in ("fixed_chars", "min", "max")}
                                               for r, w in materials["windows"].items()},
                         "organizer_sees_questionnaire_or_gold": False,
                         "organizer_sees_evaluator_dimensions": False},
        "design": {"topic_id": TOPIC_ID, "block_id": BLOCK_ID, "environment": ENVIRONMENT,
                   "subjects": list(SUBJECTS), "subject_stances": STANCES, "arms": list(ARMS),
                   "exposure_rounds": EXPOSURE_ROUNDS, "cards_per_round": 6,
                   "exposure_card_deliveries_per_subject": EXPOSURE_ROUNDS * 6,
                   "retention_rounds": RETENTION_ROUNDS, "retention_pool": "same_forum_pool_resurfacing_env_D",
                   "intervention_rounds": list(INTERVENTION_ROUNDS), "measurements": 16, "blind_evaluations": 16,
                   "registry_seed": REGISTRY_SEED, "flatten_rule": summary["flatten_rule"],
                   "prompt_revision": summary["prompt_revision"], "feedback_revision": summary["feedback_revision"],
                   "organizer_revision": summary["organizer_revision"], "condition_labels": CONDITION_LABELS,
                   "mirror_delivered_to_branches": False, "mirror_used_by_I4_organizer": True},
        "model_settings": summary["model_settings"],
        "four_stage": {"task_id": summary["four_stage_task_id"], "attempts": summary["four_stage_tasks"],
                       "outputs": summary["four_stage_outputs"],
                       "reused_from": summary.get("four_stage_reused_from")},
        "nodes": nodes, "user_views": summary["user_views"], "mirrors": summary["mirrors"],
        "mirror_citation_audit": summary["mirror_citation_audit"], "measurement_texts": measured,
        "interventions": summary["interventions"],
        "parent_t1_core_sha256": summary["parent_t1_core_sha256"], "branches": summary["branches"],
        "registry_sha256": summary["registry_sha256"], "panel_sha256": summary["panel_sha256"],
        "panel_records": [{k: r[k] for k in ("blind_sample_id", "agent_id", "arm", "timepoint", "world_id", "BS",
                                              "Acc", "S", "O", "U", "objective_complete", "row_complete",
                                              "measurement_output_sha256", "blind_evaluation_sha256")}
                          for r in panel["panel_records"]],
        "comparison": comparison, "item_level": item_level,
        "descriptive_by_arm": _descriptive(comparison, item_level),
        "totals": {"actual_model_calls": summary["actual_model_calls"], "rejected_attempts": rejected,
                   "accepted_calls": sum(1 for n in summary["nodes"] if n["accepted"]) + sum(
                       a.get("actual_model_calls", 0) for a in summary["four_stage_tasks"]
                       if a.get("status") == "succeeded"),
                   "four_stage_calls": four_calls,
                   "ledger_settles": ledger["settle_count"], "charged_cny": ledger["charged_cny"],
                   "usage": ledger["usage"], "billing_after_total_cny": summary["billing_after"]["charged_cny"],
                   "billing_after_reserved_cny": summary["billing_after"]["reserved_cny"],
                   "billing_after_remaining_cny": summary["billing_after"]["available_cny"]},
        "superseded_runs": superseded,
        "step_spend": {"final_run_charged_cny": ledger["charged_cny"],
                       "superseded_charged_cny": "%.8f" % sum(float(r["charged_cny"]) for r in superseded),
                       "step_charged_cny": "%.8f" % (float(ledger["charged_cny"])
                                                     + sum(float(r["charged_cny"]) for r in superseded))},
        "honest_gates": {"is_demo": False, "is_synthetic_closed_world": True, "protocol_basis": PROTOCOL_BASIS,
                         "real_world_evidence": False, "mixed_with_real_topics": False,
                         "sample_size_subjects": 2, "arms": 3,
                         "statistical_inference": "none_mechanism_demo_only", "human_reviews_completed": 0,
                         "full_I4_eligible": False, "approved_for_experiment": False, "formal_eligible": False,
                         "subject_is_language_model": True, "real_human_subjects": 0},
        "event_chain": summary["verification"],
    }
    manifest["manifest_sha256"] = canonical_hash(manifest)
    target = Path(manifest_path) if manifest_path is not None else repo / MANIFEST_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    _need(not target.exists(), "manifest_exists")
    write_json_new(target, manifest)
    return {"status": "written", "path": target.as_posix(), "manifest_sha256": manifest["manifest_sha256"]}


# ------------------------------------------------------------------ 离线核验
def verify_closed_world_live_pilot(manifest_path=None, *, repo_root=None, materials=None) -> dict:
    """离线重放：材料重建、事件链、blob、全部校验器、I4/I5 重组与配对、注册表/面板、账本、诚实门禁。"""
    repo = Path(repo_root).resolve() if repo_root is not None else ROOT
    path = Path(manifest_path) if manifest_path is not None else repo / MANIFEST_RELATIVE
    _need(path.is_file(), "manifest_missing")
    manifest = _parse_json(path.read_bytes())
    _need(canonical_hash({k: v for k, v in manifest.items() if k != "manifest_sha256"})
          == manifest["manifest_sha256"], "manifest_hash_mismatch")
    _need(manifest["contract"] == CONTRACT, "manifest_contract_invalid")
    materials = materials or build_closed_world_materials()
    problems = []
    if manifest["closed_world"]["materials_sha256"] != materials["materials_sha256"]:
        problems.append("materials_rebuild_mismatch")
    if manifest["closed_world"]["item_design"] != materials["item_design"]:
        problems.append("item_design_mismatch")
    run_dir = Path(manifest["run_directory"])
    summary = _parse_json((run_dir / "summary.json").read_bytes())
    if verify_events(run_dir).get("chain_valid") is not True:
        problems.append("event_chain_invalid")
    packet = materials["packet"]
    summary_nodes = {n["node_id"]: n for n in summary["nodes"]}
    records, organized = {}, {}
    for node in manifest["nodes"]:
        source = summary_nodes.get(node["node_id"])
        if source is None or not node["accepted"]:
            problems.append("node_missing_or_rejected:" + node["node_id"])
            continue
        blob = _verify_blob(run_dir, {"ref": f"raw/blobs/{node['accepted_response_sha256']}.json",
                                      "sha256": node["accepted_response_sha256"]})
        if blob["raw_text"] != source["accepted_raw_text"]:
            problems.append("raw_text_blob_mismatch:" + node["node_id"])
            continue
        kind, raw = node["kind"], blob["raw_text"]
        if kind == "subject_measurement":
            value, error, _ = lmp._measure_validator(materials, node["visible_card_ids"], node["timepoint"])(raw)
            if error:
                problems.append("measurement_revalidation_failed:" + node["node_id"])
            else:
                records[node["node_id"]] = value
        elif kind in ("routine_speech", "intervention_response"):
            low, high = lmp.SPEECH_RANGE if kind == "routine_speech" else lmp.RESPONSE_RANGE
            if lmp._plain_validator(low, high)(raw)[1] is not None or raw != node["accepted_text"]:
                problems.append("plain_text_revalidation_failed:" + node["node_id"])
        elif kind == "mirror":
            view = manifest["user_views"][node["subject"]]["user_view"]
            parsed, error, _ = lmp._mirror_validator(packet, view)(blob)
            if error or parsed["output"] != manifest["mirrors"][node["subject"]]:
                problems.append("mirror_revalidation_failed:" + node["node_id"])
        elif kind == "i4_organizer":
            value, error, _ = _organizer_validator(materials, node["intervention_round"])(raw)
            if error or raw != node["accepted_text"]:
                problems.append("organizer_revalidation_failed:" + node["node_id"])
            else:
                organized.setdefault(node["subject"], {}).update(value)
    # I4/I5/I0 可见文本由已接受组织段 + 确定性规则完整重建
    try:
        rebuilt = _intervention_record(_interventions(materials, organized))
        if rebuilt != manifest["interventions"]:
            problems.append("interventions_rebuild_mismatch")
        for subject in SUBJECTS:
            for r in INTERVENTION_ROUNDS:
                checks = rebuilt[subject]["pair_checks"][str(r)]
                if not all(checks[k] for k in extractive_control._CHECK_NAMES):
                    problems.append(f"pair_checks_failed:{subject}:R{r}")
        for node in manifest["nodes"]:
            if node["kind"] == "intervention_response":
                expected = rebuilt[node["subject"]][node["arm"]][str(node["intervention_round"])]
                if expected["visible_text_sha256"] != node["intervention_sha256"]:
                    problems.append("intervention_hash_mismatch:" + node["node_id"])
    except (KeyError, TypeError) as exc:
        problems.append("interventions_rebuild_error:" + str(exc)[:80])
    for subject in SUBJECTS:
        view = manifest["user_views"][subject]
        if _sha_text(view["user_view"]) != view["user_view_sha256"]:
            problems.append("user_view_hash_mismatch:" + subject)
        speech = next(n for n in manifest["nodes"] if n["node_id"] == f"speech_{subject}_T1")
        if speech["accepted_text"] not in view["user_view"]:
            problems.append("user_view_not_from_speech:" + subject)
    try:
        entries = [{"parsed_measurement": records[n["node_id"]], "gold_bundle": materials["gold"],
                    "world_id": n["world_id"], "agent_id": n["subject"], "environment": ENVIRONMENT, "arm": n["arm"],
                    "timepoint": n["timepoint"], "block_id": BLOCK_ID}
                   for n in manifest["nodes"] if n["kind"] == "subject_measurement" and n["node_id"] in records]
        registry = measurement.create_blind_sample_registry(entries, registry_seed=manifest["design"]["registry_seed"])
        if registry["registry_sha256"] != manifest["registry_sha256"]:
            problems.append("registry_rebuild_mismatch")
        evaluations = []
        for blind_packet in registry["public_blind_packets"]:
            node = next(n for n in manifest["nodes"] if n.get("blind_sample_id") == blind_packet["blind_sample_id"])
            blob = _verify_blob(run_dir, {"ref": f"raw/blobs/{node['accepted_response_sha256']}.json",
                                          "sha256": node["accepted_response_sha256"]})
            evaluations.append(measurement.parse_blind_evaluation_output(blob["raw_text"], blind_packet))
        by_id = {sid: registry["source_entries"][b["source_entry_index"]]["parsed_measurement"]
                 for sid, b in registry["private_mapping"].items()}
        panel = measurement.reconcile_blind_evaluations(registry, evaluations, by_id, materials["gold"],
                                                        expected_registry_sha256=registry["registry_sha256"])
        if panel["panel_sha256"] != manifest["panel_sha256"]:
            problems.append("panel_rebuild_mismatch")
        comparison = lmp._table(panel["panel_records"], SUBJECTS)
        if comparison != manifest["comparison"]:
            problems.append("comparison_table_mismatch")
        if panel["complete_panel_records"] != 16:
            problems.append("panel_incomplete")
        item_level = _item_level(summary["nodes"])
        if item_level != manifest["item_level"]:
            problems.append("item_level_mismatch")
        if _descriptive(comparison, item_level) != manifest["descriptive_by_arm"]:
            problems.append("descriptive_mismatch")
    except (ValueError, KeyError, StopIteration) as exc:
        problems.append("registry_or_panel_error:" + str(exc)[:120])
    for branch in manifest["branches"]:
        if branch["pre_treatment_core_sha256"] != manifest["parent_t1_core_sha256"][branch["subject"]]:
            problems.append("branch_parent_hash_mismatch")
    reused = manifest["four_stage"].get("reused_from")
    if reused is not None:
        try:
            again = load_four_stage_source(materials, {"run_id": reused["run_id"], "task_id": reused["task_id"]},
                                           repo_root=repo)
            if again["provenance"] != reused:
                problems.append("four_stage_reuse_provenance_mismatch")
            if {k: v["structured_output"] for k, v in again["previous"].items()} != manifest["four_stage"]["outputs"]:
                problems.append("four_stage_reuse_output_mismatch")
            if not any(r["run_id"] == reused["run_id"] for r in manifest.get("superseded_runs") or []):
                problems.append("four_stage_reuse_source_cost_not_disclosed")
        except (ValueError, KeyError, StopIteration, OSError) as exc:
            problems.append("four_stage_reuse_invalid:" + str(exc)[:80])
    rows = lmp._ledger_rows(repo)
    ledger = lmp._ledger_totals(rows, manifest["run_id"])
    if ledger["charged_cny"] != manifest["totals"]["charged_cny"]:
        problems.append("ledger_charge_mismatch")
    if ledger["settle_count"] != manifest["totals"]["ledger_settles"]:
        problems.append("ledger_settle_count_mismatch")
    if manifest["totals"]["actual_model_calls"] != summary["actual_model_calls"]:
        problems.append("call_count_mismatch")
    if _superseded_runs(repo, rows, manifest["run_id"]) != manifest.get("superseded_runs"):
        problems.append("superseded_runs_mismatch")
    gates = manifest["honest_gates"]
    for key, expected in (("formal_eligible", False), ("approved_for_experiment", False),
                          ("full_I4_eligible", False), ("human_reviews_completed", 0), ("is_demo", False),
                          ("is_synthetic_closed_world", True), ("real_world_evidence", False),
                          ("mixed_with_real_topics", False)):
        if gates.get(key) is not expected:
            problems.append("honest_gate_" + key)
    if gates.get("protocol_basis") != PROTOCOL_BASIS or manifest["closed_world"].get("protocol_basis") != PROTOCOL_BASIS:
        problems.append("honest_gate_protocol_basis")
    if manifest["closed_world"].get("is_synthetic_closed_world") is not True:
        problems.append("closed_world_flag_missing")
    return {"contract": CONTRACT, "topic_id": TOPIC_ID, "passed": not problems, "problems": problems,
            "manifest_sha256": manifest["manifest_sha256"], "run_id": manifest["run_id"],
            "measurements_revalidated": len(records),
            "organizer_outputs_revalidated": sum(len(v) for v in organized.values()),
            "panel_records": len(manifest["panel_records"]), "charged_cny": ledger["charged_cny"],
            "actual_model_calls": manifest["totals"]["actual_model_calls"],
            "is_synthetic_closed_world": True, "protocol_basis": PROTOCOL_BASIS, "formal_eligible": False,
            "verification_scope": "本地材料重建/事件链/blob/校验器/配对/注册表/账本重算；不验证语义质量。"}


def summarize_closed_world_live_pilot(repo_root=None, *, manifest_path=None, materials=None) -> dict:
    """供看板使用的只读摘要；清单缺失或核验失败时返回不可用。"""
    repo = Path(repo_root).resolve() if repo_root is not None else ROOT
    path = Path(manifest_path) if manifest_path is not None else repo / MANIFEST_RELATIVE
    empty = {"available": False, "reason": "manifest_absent", "path": path.as_posix(), "topic_id": TOPIC_ID,
             "is_synthetic_closed_world": True, "actual_model_calls": 0, "charged_cny": "0.00000000",
             "subjects": [], "comparison": [], "descriptive_by_arm": []}
    if not path.is_file():
        return empty
    try:
        check = verify_closed_world_live_pilot(path, repo_root=repo, materials=materials)
    except (ValueError, KeyError, OSError, TypeError):
        return {**empty, "reason": "manifest_verification_failed"}
    if not check["passed"]:
        return {**empty, "reason": "manifest_not_passed", "problems": check["problems"]}
    m = _parse_json(path.read_bytes())
    subjects = []
    for subject in SUBJECTS:
        speech = next(n for n in m["nodes"] if n["node_id"] == f"speech_{subject}_T1")
        mirror = m["mirrors"][subject]
        organizer = [n for n in m["nodes"] if n["kind"] == "i4_organizer" and n["subject"] == subject]
        subjects.append({
            "subject": subject, "initial_side": "positive" if STANCES[subject] > 0 else "negative",
            "speech": speech["accepted_text"],
            "mirror_items": [{"section": s["name"], "text": it["text"], "user_quote": it.get("user_quote"),
                              "citations": it["citations"]} for s in mirror["sections"] for it in s["items"]],
            "i4_additions": {r: v["visible_text"].split("\n\n阅读说明\n", 1)[1].rsplit("\n\n", 1)[0]
                             for r, v in m["interventions"][subject]["I4"].items()},
            "i5_additions": {r: v["visible_text"].split("\n\n阅读说明\n", 1)[1].rsplit("\n\n", 1)[0]
                             for r, v in m["interventions"][subject]["I5"].items()},
            "pair_checks": m["interventions"][subject]["pair_checks"],
            "organizer_attempts": sum(len(n["attempts"]) for n in organizer),
            "intervention_responses": {b["arm"]: b["responses"] for b in m["branches"] if b["subject"] == subject},
            "t1_brief_reasons": m["measurement_texts"][f"measure_{subject}_P_T1"]["brief_reasons"],
            "t3_brief_reasons": {arm: m["measurement_texts"][f"measure_{subject}_{arm}_T3"]["brief_reasons"]
                                 for arm in ARMS},
            "t3_opposing_view": {arm: m["measurement_texts"][f"measure_{subject}_{arm}_T3"]["opposing_view"]
                                 for arm in ARMS}})
    trace = [{"node_id": n["node_id"], "kind": n["kind"], "subject": n.get("subject"), "arm": n.get("arm"),
              "timepoint": n.get("timepoint"), "attempts": len(n["attempts"]),
              "rejected": sum(a["status"] == "rejected" for a in n["attempts"]),
              "event_id": n.get("accepted_event_id"), "response_sha256": n["accepted_response_sha256"]}
             for n in m["nodes"]]
    return {"available": True, "reason": "verified", "path": path.as_posix(), "topic_id": TOPIC_ID,
            "topic_label": TOPIC_LABEL, "is_synthetic_closed_world": True, "protocol_basis": PROTOCOL_BASIS,
            "protocol_note": PROTOCOL_NOTE, "manifest_sha256": m["manifest_sha256"], "run_id": m["run_id"],
            "materials_sha256": m["closed_world"]["materials_sha256"],
            "item_design": m["closed_world"]["item_design"], "exposure_audit": m["closed_world"]["exposure_audit"],
            "misled_anchor_items": m["closed_world"]["misled_anchor_items"],
            "actual_model_calls": m["totals"]["actual_model_calls"],
            "rejected_attempts": m["totals"]["rejected_attempts"], "charged_cny": m["totals"]["charged_cny"],
            "usage": m["totals"]["usage"], "comparison": m["comparison"], "item_level": m["item_level"],
            "descriptive_by_arm": m["descriptive_by_arm"], "subjects": subjects, "trace": trace,
            "honest_gates": m["honest_gates"], "registry_sha256": m["registry_sha256"],
            "panel_sha256": m["panel_sha256"], "superseded_runs": m.get("superseded_runs", []),
            "step_spend": m.get("step_spend"), "billing_after_total_cny": m["totals"]["billing_after_total_cny"]}


def _load_config(repo):
    from .configuration import load_config
    return load_config(Path(repo) / "configs/core.json")
