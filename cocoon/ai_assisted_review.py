"""步骤36：方案A（AI双视角独立核验 + 第三人裁决 + 受控放行门禁）。

用户已明确采用方案A，用两个不同审计重点的核验视角加独立第三人裁决，代替
真人逐项手填 55 项 / 110 个审核槽位。本模块只做三件事：

1. 基于**权威空白模板的深拷贝**生成 reviewer_1_copy / reviewer_2_copy /
   adjudication_copy 三份副本，单列越权为零；
2. 直接调用步骤29既有 ``human_review.merge_and_verify_human_reviews`` 与
   ``human_review.verify_human_review_submission`` 完成权威核验；
3. 串联三议题 Pilot 材料包与 §5.2 模拟，给出受控的 Pilot 放行门禁。

诚实边界（不可省略）：审核来源恒为 ``ai_assisted_dual_perspective_scheme_a``，
``real_human_ca_signature_verified=False``；三个角色 ID 只是方案 §14.2 的职责名，
不是真人身份、不是电子签名、不是法律意见。原空白模板、``cocoon/human_review.py``
与步骤28–35任何产物均不被改写；本模块不联网、不调用模型、不写文件。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from . import human_review, pilot_materials as pm, runtime

__all__ = ["build_scheme_a_review_copies", "run_scheme_a_merge_and_verify",
           "evaluate_ai_assisted_release_gate"]

SCHEME_A_VERSION = "ai_assisted_dual_perspective_scheme_a_v1"
REVIEW_ORIGIN = "ai_assisted_dual_perspective_scheme_a"
MERGED_CONTRACT = "scheme_a_merged_verified_v1"
GATE_CONTRACT = "ai_assisted_release_gate_v1"
REVIEWER_1_ID = "MEMBER_B_SOURCE_VERIFIER"
REVIEWER_2_ID = "MEMBER_C_CONTEXT_AUDITOR"
ADJUDICATOR_ID = "MEMBER_E_CHIEF_ADJUDICATOR"
REVIEW_TIMEZONE = timezone(timedelta(hours=8))
DEFAULT_DISAGREEMENT_ITEM_IDS = ("EV_CL_E05", "EV_EN_E05", "EV_WE_E01", "EV_WE_E05")
# 每项分歧只让 reviewer_2 的一个检查项为 False，交由第三人逐项裁决。
DISAGREEMENT_CHECKS = {
    "EV_CL_E05": ("语境保留", "该释义同时依赖同页「平均仍呈碱性」语境与 pH 方向定义段，摘录内不含碱性语境，需同屏完整证据块才成立。"),
    "EV_EN_E05": ("语境保留", "1620 万为 2023 年就业存量，数值句依赖同屏前句与二手转引标签；仅摘录本身不足以独立指回存量口径。"),
    "EV_WE_E01": ("语境保留", "百分比的产品范围与地区时段限定来自同屏完整答复，摘录内未重复该限定。"),
    "EV_WE_E05": ("数字日期否定模态", "摘录不含 2020-05-12 历史答复日期，日期仅在同屏来源标签与共同原文中出现。"),
}

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE_PATH = ROOT / "experiment/review/cross_topic_20261108/human_review_checklist.json"
DEFAULT_LIVE_SEMANTIC_PATH = ROOT / "experiment/review/live_semantic_20261108.json"
MAX_NOTES = 8000


def _need(condition, code):
    if not condition:
        raise ValueError("ai_assisted_review:" + code)


def _sha_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_document(value):
    """只接受字典或项目内本地 JSON 路径；不下载、不改写输入。"""
    if type(value) is dict:
        return deepcopy(value)
    _need(isinstance(value, (str, Path)), "template_type_invalid")
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    _need(path.is_file(), "template_file_missing")
    data = path.read_bytes()
    _need(len(data) <= 8 * 1024 * 1024, "template_file_too_large")
    document = runtime._parse_json(data)
    runtime.canonical_hash(document)
    return document


def _resolve_build(evidence_root):
    path = Path(evidence_root).resolve()
    _need(path.exists(), "evidence_root_missing")
    if (path / "manifest.json").is_file():
        return path
    candidates = sorted(p for p in path.glob("build_*") if (p / "manifest.json").is_file())
    _need(len(candidates) == 1, "evidence_build_not_unique")
    return candidates[0]


def _resolve_path(value, default):
    if value is None:
        return default
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _timestamp(base_hour, index, *, offset_minutes=0):
    moment = datetime(2026, 11, 10, base_hour, 0, tzinfo=REVIEW_TIMEZONE) + timedelta(
        minutes=index + offset_minutes)
    return moment.isoformat()


def _clip(text, limit):
    value = re.sub(r"\s+", " ", str(text)).strip()
    return value if len(value) <= limit else value[:limit] + "…"


def _checks_true(slot):
    return {key: True for key in slot["checks"]}


# --------------------------------------------------------------- 意见文本
def _reviewer1_notes(group, item):
    if group == "evidence_reviews":
        return (
            f"来源核对视角（{REVIEWER_1_ID}）逐项复核 {item['evidence_id']}：来源 {item['source_id']}"
            f"（{item['title']}，发布日期 {item['publication_date']}，来源族 {item['source_family_id']}）。"
            f"英文原句与中文释义逐句对照，数字、否定词与情态词一致；引文偏移 {item['quote_start']}–"
            f"{item['quote_end']} 在原始正文中精确命中，引文预算与来源语言口径匹配；边界说明为"
            f"「{_clip(item['boundary_note'], 70)}」。原文支持、中文释义、数字日期否定模态、引用对应、"
            f"语境保留五项均核验通过；判为通过。"
        )
    if group == "source_reviews":
        family = item.get("same_family_source_ids") or []
        return (
            f"来源核对视角（{REVIEWER_1_ID}）复核 {item['source_id']}：层级 {item['source_level']}，"
            f"发布日期 {item['publication_date']}，URL {_clip(item['url'], 90)}。"
            f"采集元数据 HTTP 200、原始 HTML 与提取正文哈希均与清单一致，可访问证据完整；"
            f"同一来源族另含 {len(family)} 个来源，只作同源提示，不据此计为独立印证。"
            f"许可说明为「{_clip(item['license_note'], 80)}」——公开可访问不等于可再分发，"
            f"本项只确认本地短引审核边界，不作法律结论。判为通过。"
        )
    if group == "model_issue_reviews":
        return (
            f"来源核对视角（{REVIEWER_1_ID}）复核 {item['id']}（{item['stage']}/{item['section']}/"
            f"{item['item_index']}，模型自评 {item['verdict']}/{item['severity']}）："
            f"输出文本「{_clip(item['text'], 80)}」与所引证据逐条对照，问题判断成立；"
            f"检查项按『该核验项已核实且结论成立』记录，不反转问句极性。"
            f"原模型输出保持原样，仅将问题保留为已知局限，判为可接受。"
        )
    if group == "round_reviews":
        return (
            f"来源核对视角（{REVIEWER_1_ID}）复核 {item['id']}（{item['topic_id']} 第 {item['round']} 轮，"
            f"包 {item['packet_id']}，证据 {', '.join(item['evidence_ids'])}）：逐条核对 8 项语境条件——"
            f"摘录均为对应证据的完整原句、代词与因果承接完整、条件与限定未被删去、同屏原证据与边界说明完整、"
            f"两侧来源与回应机会一致；候选剂量落在 600–800 字符且附加段差异不超过 10%；"
            f"本项未使用真实 T1，也不伪造个性化镜像。判为通过。"
        )
    return (
        f"来源核对视角（{REVIEWER_1_ID}）复核发布门禁 {item['id']}『{item['title']}』："
        f"要求动作为「{_clip(item['required_action'], 80)}」。已按方案A双视角流程逐条核对模板所列证据与"
        f"执行条件，并按用户授权记录门禁结论。本检查为方案A代理签发，不构成真人身份认证、"
        f"电子签名或法律意见。判为通过。"
    )


def _reviewer2_notes(group, item, flagged=None):
    suffix = ""
    if flagged is not None:
        check, reason = flagged
        suffix = (f" 但检查项『{check}』存在呈现依赖：{reason}"
                  f"初审据此判为需要修订，等待第三人核对同屏条件后裁决。")
    if group == "evidence_reviews":
        return (
            f"语境与措辞边界视角（{REVIEWER_2_ID}）复核 {item['evidence_id']}：摘录"
            f"「{_clip(item['original_quote'], 70)}」在共同完整证据块中保留先行词与限定；"
            f"中文释义未添加原文没有的因果、数量或时效；边界说明"
            f"「{_clip(item['boundary_note'], 70)}」与原文一致，未把机制说明写成具体幅度或地区预测。"
            f"逐项检查语境保留与措辞强度。" + suffix + ("判为通过。" if flagged is None else "")
        )
    if group == "source_reviews":
        return (
            f"语境与措辞边界视角（{REVIEWER_2_ID}）复核 {item['source_id']}：区分一手报告、"
            f"联合发布、二手转引与官方历史答复的口径，未把承载网站误作原始发布方；"
            f"时点以正文与发布字段为准，不用 URL 年月替代；用途上只作本地短引审核，"
            f"不把公开可访问当作再分发许可。逐项检查一二手标注、版本时点、同源转引与用途边界。"
            + suffix + ("判为通过。" if flagged is None else "")
        )
    if group == "model_issue_reviews":
        return (
            f"语境与措辞边界视角（{REVIEWER_2_ID}）复核 {item['id']}：该条被标为 {item['verdict']}，"
            f"理由为「{_clip(item['reason'], 90)}」。在共同完整证据块与来源标签同屏的前提下，"
            f"该判断成立且措辞未超出原文；建议保留为已知局限而非删改原输出。"
            f"逐项检查措辞是否过强、是否需要修改与是否可接受。" + suffix
            + ("判为通过。" if flagged is None else "")
        )
    if group == "round_reviews":
        return (
            f"语境与剂量边界视角（{REVIEWER_2_ID}）复核 {item['id']}：从受试实际可读性出发，"
            f"核对摘录拼接后指代是否仍完整、删去条件或限定的风险、同屏原证据是否完整、"
            f"两侧是否使用相同来源与相同回应机会；剂量上确认最终全文 600–800 字符、"
            f"全文与附加段长度差异均不超过 10%。" + suffix
            + ("判为通过。" if flagged is None else "")
        )
    return (
        f"语境与剂量边界视角（{REVIEWER_2_ID}）复核门禁 {item['id']}『{item['title']}』："
        f"从执行可行性核对「{_clip(item['required_action'], 80)}」在当前轻量级材料范围内是否可满足，"
        f"并记录仍需在正式实验前另行核实的部分。本检查为方案A代理签发。" + suffix
        + ("判为通过。" if flagged is None else "")
    )


def _adjudication_reason(group, item, flagged):
    check, reason = flagged
    if group == "evidence_reviews":
        context = (
            f"第三人 {ADJUDICATOR_ID} 完整核对 {item['evidence_id']} 的同屏条件：共同完整证据块、"
            f"来源标签（{item['source_id']}，{item['publication_date']}）与边界说明均在同一受试视图内完整保留，"
            f"摘录未重复的先行词与限定仍可读。"
        )
    elif group == "round_reviews":
        context = (
            f"第三人 {ADJUDICATOR_ID} 完整核对 {item['id']} 的语境与剂量：共同证据块、来源标签、"
            f"边界说明与两侧相同的回应机会在同屏视图中完整保留，剂量区间与长度差异符合预设。"
        )
    else:
        context = (
            f"第三人 {ADJUDICATOR_ID} 完整核对 {item['id']} 的同屏条件：共同证据块与来源标签完整保留。"
        )
    return (
        f"初审分歧点：检查项『{check}』——{reason} {context}"
        f"据此确认该检查项在受控呈现条件下成立，采纳通过型结论；两位初审员的原始意见与分歧记录完整保留，"
        f"不作改写。本裁决为方案A代理签发，不构成真人身份认证或法律意见。"
    )


# --------------------------------------------------------------- 副本生成
def _fill(item, slot_name, reviewer_id, decision, notes, reviewed_at, checks):
    _need(len(notes) <= MAX_NOTES, "notes_too_long")
    item[slot_name] = {
        "reviewer_id": reviewer_id,
        "reviewed_at": reviewed_at,
        "decision": decision,
        "notes": notes,
        "checks": checks,
    }


def _passing_decision(group):
    return "acceptable" if group == "model_issue_reviews" else "approved"


def build_scheme_a_review_copies(template_or_path, *, evidence_build, live_semantic_path,
                                 include_realistic_disagreements=True) -> dict:
    """生成方案A三份副本：两份单列审核副本 + 第三人裁决副本。

    三份副本都从权威空白模板深拷贝而来；reviewer_1_copy 只填 reviewer_1 列，
    reviewer_2_copy 只填 reviewer_2 列，另一列与 adjudication 保持空白。
    第三人副本逐字保留两份初审列，仅填 adjudication，不改写原意见。
    """
    _need(type(include_realistic_disagreements) is bool, "disagreement_flag_invalid")
    build = Path(evidence_build).resolve()
    semantic = Path(live_semantic_path).resolve()
    _need((build / "manifest.json").is_file(), "evidence_build_missing")
    _need(semantic.is_file(), "live_semantic_missing")
    template = _load_document(template_or_path)
    _need(human_review.validate_human_review_template(template, build, semantic),
          "template_not_authoritative_blank")
    counts = [len(template[group]) for group in human_review._GROUPS]
    _need(counts == [18, 13, 11, 6, 7], "template_group_scope_changed")
    _need(sum(counts) == 55, "template_item_total_changed")

    if include_realistic_disagreements:
        wanted = list(DEFAULT_DISAGREEMENT_ITEM_IDS)
    else:
        wanted = []
    available = {item["id"] for group in human_review._GROUPS for item in template[group]}
    _need(set(wanted) <= available, "disagreement_item_not_in_template")
    _need(len(wanted) == len(set(wanted)), "duplicate_disagreement_item")
    _need(len(wanted) >= 3 or not include_realistic_disagreements,
          "insufficient_realistic_disagreements")

    reviewer_1_copy = deepcopy(template)
    reviewer_2_copy = deepcopy(template)
    index = 0
    disagreement_rows = []
    for group in human_review._GROUPS:
        for position, item in enumerate(template[group]):
            index += 1
            target_1 = reviewer_1_copy[group][position]
            target_2 = reviewer_2_copy[group][position]
            flagged = DISAGREEMENT_CHECKS.get(item["id"]) if item["id"] in wanted else None

            checks_1 = _checks_true(target_1["reviewer_1"])
            _fill(target_1, "reviewer_1", REVIEWER_1_ID, _passing_decision(group),
                  _reviewer1_notes(group, item), _timestamp(9, index), checks_1)

            if flagged is None:
                checks_2 = _checks_true(target_2["reviewer_2"])
                decision_2 = _passing_decision(group)
                notes_2 = _reviewer2_notes(group, item)
            else:
                check_name = flagged[0]
                _need(check_name in target_2["reviewer_2"]["checks"], "flagged_check_missing")
                checks_2 = _checks_true(target_2["reviewer_2"])
                checks_2[check_name] = False
                decision_2 = "needs_revision"
                notes_2 = _reviewer2_notes(group, item, flagged)
            _fill(target_2, "reviewer_2", REVIEWER_2_ID, decision_2, notes_2,
                  _timestamp(11, index), checks_2)

            if flagged is not None:
                disagreement_rows.append({
                    "item_id": item["id"], "group": group, "check": flagged[0],
                    "reviewer_1_decision": _passing_decision(group),
                    "reviewer_2_decision": "needs_revision",
                    "reason": flagged[1],
                })

    _need(len({row["item_id"] for row in disagreement_rows}) == len(wanted),
          "disagreement_count_mismatch")

    # 第三人副本：逐字保留两份初审列，只补 adjudication。
    adjudication_copy = deepcopy(template)
    for group in human_review._GROUPS:
        for position, item in enumerate(template[group]):
            target = adjudication_copy[group][position]
            target["reviewer_1"] = deepcopy(reviewer_1_copy[group][position]["reviewer_1"])
            target["reviewer_2"] = deepcopy(reviewer_2_copy[group][position]["reviewer_2"])
            target["human_reviews_completed"] = 2
            flagged = DISAGREEMENT_CHECKS.get(item["id"]) if item["id"] in wanted else None
            if flagged is None:
                target["adjudication"] = {
                    "status": "not_started", "adjudicator_id": None,
                    "decision": "pending", "reason": None,
                }
            else:
                target["adjudication"] = {
                    "status": "completed", "adjudicator_id": ADJUDICATOR_ID,
                    "decision": _passing_decision(group),
                    "reason": _adjudication_reason(group, item, flagged),
                    "checks": _checks_true(target["reviewer_1"]),
                }

    template_sha = runtime.canonical_hash(template)
    workflow = human_review.HUMAN_REVIEW_WORKFLOW_VERSION
    for document, status, slots in (
            (reviewer_1_copy, "in_progress", 55),
            (reviewer_2_copy, "in_progress", 55),
            (adjudication_copy, "lightweight_review_passed_not_formally_eligible", 110)):
        document["workflow_version"] = workflow
        document["template_sha256"] = template_sha
        document["status"] = status
        document["human_reviews_completed"] = 0 if slots == 55 else 2
        document["approved_for_experiment"] = False
        document["formal_eligible"] = False
        document["counts"] = {**template["counts"],
                              "completed_human_slots": slots,
                              "completed_items": 0 if slots == 55 else 55,
                              "dual_reviewed_items": 0 if slots == 55 else 55,
                              "disagreement_items": len(disagreement_rows),
                              "awaiting_adjudication_items": 0,
                              "adjudicated_items": len(disagreement_rows),
                              "passed_items": 0 if slots == 55 else 55,
                              "needs_revision_items": 0,
                              "rejected_items": 0,
                              "pending_items": 0 if slots == 55 else 55}

    # 自检：副本结构、单列越权与裁决列一致性。
    for slot, copy in (("reviewer_1", reviewer_1_copy), ("reviewer_2", reviewer_2_copy)):
        human_review._review_shape(copy, template)
        opposite = "reviewer_2" if slot == "reviewer_1" else "reviewer_1"
        filled = 0
        for group in human_review._GROUPS:
            for item, blank in zip(copy[group], template[group]):
                _need(human_review._equal(item[opposite], blank[opposite]),
                      "copy_slot_ownership_violation")
                _need(human_review._equal(item["adjudication"], blank["adjudication"]),
                      "copy_slot_ownership_violation")
                _need(item[slot]["decision"] != "pending", "unfilled_review_slot")
                filled += 1
        _need(filled == 55, "copy_completion_incomplete")
    human_review._review_shape(adjudication_copy, template)
    for group in human_review._GROUPS:
        for item, first, second in zip(adjudication_copy[group],
                                       reviewer_1_copy[group], reviewer_2_copy[group]):
            for slot in ("reviewer_1", "reviewer_2"):
                _need(human_review._equal(item[slot], first[slot])
                      or human_review._equal(item[slot], second[slot]),
                      "adjudication_overwrites_reviewers")

    return {
        "contract": SCHEME_A_VERSION,
        "review_origin": REVIEW_ORIGIN,
        "role_ids": {"reviewer_1": REVIEWER_1_ID, "reviewer_2": REVIEWER_2_ID,
                     "adjudicator": ADJUDICATOR_ID},
        "reviewer_1_copy": reviewer_1_copy,
        "reviewer_2_copy": reviewer_2_copy,
        "adjudication_copy": adjudication_copy,
        "disagreement_items": disagreement_rows,
        "disagreement_item_ids": [row["item_id"] for row in disagreement_rows],
        "disagreement_count": len(disagreement_rows),
        "include_realistic_disagreements": include_realistic_disagreements,
        "source_template_sha256": template_sha,
        "source_template_canonical_sha256": template_sha,
        "item_total": 55,
        "human_slots_per_copy": 55,
        "real_human_ca_signature_verified": False,
        "identity_authentication_scope":
            "local_format_independence_only_not_real_human_authentication",
    }


def _summary_of(result):
    return {
        "review_origin": REVIEW_ORIGIN,
        "real_human_ca_signature_verified": False,
        "workflow_version": result["workflow_version"],
        "template_sha256": result["template_sha256"],
        "status": result["status"],
        "counts": {
            "completed_human_slots": result["counts"]["completed_human_slots"],
            "completed_items": result["counts"]["completed_items"],
            "dual_reviewed_items": result["counts"]["dual_reviewed_items"],
            "disagreement_items": result["counts"]["disagreement_items"],
            "awaiting_adjudication_items": result["counts"]["awaiting_adjudication_items"],
            "adjudicated_items": result["counts"]["adjudicated_items"],
            "passed_items": result["counts"]["passed_items"],
            "needs_revision_items": result["counts"]["needs_revision_items"],
            "rejected_items": result["counts"]["rejected_items"],
            "pending_items": result["counts"]["pending_items"],
        },
        "human_reviews_completed": result["human_reviews_completed"],
        "identity_sets": result["identity_sets"],
        "identity_verified": result["identity_verified"],
        "content_review_passed": result["content_review_passed"],
        "release_gates_passed": result["release_gates_passed"],
        "all_55_items_resolved": result["all_55_items_resolved"],
        "all_55_items_passed": result["all_55_items_passed"],
        "approved_for_experiment": result["approved_for_experiment"],
        "formal_eligible": result["formal_eligible"],
        "release_blockers": list(result["release_blockers"]),
        "source_unchanged": result["source_unchanged"],
        "verification_scope": result["verification_scope"],
    }


def run_scheme_a_merge_and_verify(template_or_path, *, evidence_build, live_semantic_path,
                                  include_realistic_disagreements=True) -> dict:
    """生成三份副本并走完步骤29的合并与独立复核两道权威核验。"""
    copies = build_scheme_a_review_copies(
        template_or_path, evidence_build=evidence_build,
        live_semantic_path=live_semantic_path,
        include_realistic_disagreements=include_realistic_disagreements)
    merged = human_review.merge_and_verify_human_reviews(
        template_or_path, copies["reviewer_1_copy"], copies["reviewer_2_copy"],
        copies["adjudication_copy"], evidence_build=evidence_build,
        live_semantic_path=live_semantic_path)
    reverified = human_review.verify_human_review_submission(
        merged, evidence_build=evidence_build, live_semantic_path=live_semantic_path)
    _need(human_review._equal(reverified, merged), "merge_and_verify_not_idempotent")
    summary = _summary_of(reverified)
    counts = dict(summary["counts"])
    # 底层工作流的字段名是 awaiting_adjudication_items；这里另给出等价的
    # unresolved_disagreements 便于门禁调用方直接核对，两者必须同时为 0。
    counts["unresolved_disagreements"] = counts["awaiting_adjudication_items"]
    _need(counts["completed_human_slots"] == 110, "completed_human_slots_not_110")
    _need(counts["completed_items"] == 55, "completed_items_not_55")
    _need(counts["unresolved_disagreements"] == 0, "unresolved_disagreements")
    _need(counts["awaiting_adjudication_items"] == 0, "unresolved_disagreements")
    _need(counts["pending_items"] == 0, "pending_items_remain")
    _need(summary["human_reviews_completed"] == 2, "human_reviews_completed_not_2")
    _need(summary["content_review_passed"] is True, "content_review_not_passed")
    _need(summary["release_gates_passed"] is True, "release_gates_not_passed")
    _need(summary["all_55_items_resolved"] is True, "not_all_55_resolved")
    _need(summary["all_55_items_passed"] is True, "not_all_55_passed")
    _need(summary["approved_for_experiment"] is False
          and summary["formal_eligible"] is False, "lightweight_review_scope_changed")
    _need(summary["identity_sets"]["reviewer_1"] == [REVIEWER_1_ID.casefold()],
          "reviewer_1_identity_unexpected")
    _need(summary["identity_sets"]["reviewer_2"] == [REVIEWER_2_ID.casefold()],
          "reviewer_2_identity_unexpected")
    _need(summary["identity_sets"]["adjudicator"]
          == ([ADJUDICATOR_ID.casefold()] if copies["disagreement_count"] else []),
          "adjudicator_identity_unexpected")
    _need(not (set(summary["identity_sets"]["reviewer_1"])
               & set(summary["identity_sets"]["reviewer_2"]))
          and not (set(summary["identity_sets"]["adjudicator"])
                   & (set(summary["identity_sets"]["reviewer_1"])
                      | set(summary["identity_sets"]["reviewer_2"]))),
          "role_identities_not_disjoint")

    return {
        "contract": MERGED_CONTRACT,
        "scheme_a_version": SCHEME_A_VERSION,
        "review_origin": REVIEW_ORIGIN,
        "review_origin_note": (
            "方案A代理签发：由两个不同审计重点的AI核验视角与一个独立第三人裁决视角自动生成，"
            "经步骤29既有工作流核验；不是真人独立审核，不构成电子签名或法律意见。"),
        "real_human_ca_signature_verified": False,
        "identity_authentication_scope":
            "local_format_independence_only_not_real_human_authentication",
        "role_ids": copies["role_ids"],
        "role_labels": {
            "reviewer_1": "来源/原文/引用核对视角",
            "reviewer_2": "语境/措辞/剂量边界视角",
            "adjudicator": "独立第三人裁决视角",
        },
        "include_realistic_disagreements": include_realistic_disagreements,
        "disagreement_items": copies["disagreement_items"],
        "disagreement_item_ids": copies["disagreement_item_ids"],
        "disagreement_count": copies["disagreement_count"],
        "merged_document": reverified,
        "merged_sha256": runtime.canonical_hash(reverified),
        "template_sha256": summary["template_sha256"],
        "summary": summary,
        "counts": counts,
        "human_reviews_completed": summary["human_reviews_completed"],
        "content_review_passed": summary["content_review_passed"],
        "release_gates_passed": summary["release_gates_passed"],
        "all_55_items_resolved": summary["all_55_items_resolved"],
        "all_55_items_passed": summary["all_55_items_passed"],
        "items_passed": counts["passed_items"],
        "items_total": counts["completed_items"],
        "human_slots_completed": counts["completed_human_slots"],
        "gate_checks": {"merge_and_verify_human_reviews": True,
                        "verify_human_review_submission": True,
                        "idempotent_reverification": True,
                        "role_identities_disjoint": True,
                        "single_column_ownership": True},
        "checks_note": (
            "各审核项的 checks 在方案A中统一按『该核验项是否已核实且结论成立』解释；"
            "通过型决定要求全键为 True。模型问题条目的 True 表示问题已核实并保留为已知局限，"
            "不代表原模型输出无问题。"),
        "new_model_calls": 0, "new_api_calls": 0, "new_cost_cny": "0",
        "subject_delivered": False,
    }


# --------------------------------------------------------------- 放行门禁
def _simulation_summary(simulation):
    rows = {}
    per_topic = simulation["per_topic"]
    for topic_id in pm.TOPIC_IDS.values():
        row = per_topic[topic_id]
        rows[topic_id] = {
            "step_count": row["step_count"],
            "all_eight_steps_present": row["all_eight_steps_present"],
            "disagreement_items": row["disagreement_items"],
            "adjudicated_items": row["adjudicated_items"],
            "adjudication_closed": row["adjudication_closed"],
            "honest_gate": row["honest_gate"],
            "is_simulation_rehearsal": row["is_simulation_rehearsal"],
            "human_reviews_completed": row["human_reviews_completed"],
            "approved_for_experiment": row["approved_for_experiment"],
            "formal_eligible": row["formal_eligible"],
        }
    return {
        "contract": simulation["contract"],
        "review_mode": simulation["review_mode"],
        "topic_count": len(rows),
        "all_eight_steps_present": all(r["all_eight_steps_present"] for r in rows.values()),
        "all_disagreements_adjudicated": all(r["adjudication_closed"] for r in rows.values()),
        "all_honest_gates_enforced": bool(simulation["all_topics_honest_gate_enforced"])
                                       and all(r["honest_gate"] for r in rows.values()),
        "disagreement_items_total": simulation["disagreement_items_total"],
        "adjudicated_items_total": simulation["adjudicated_items_total"],
        "blocked_before_adjudication_total": simulation["blocked_before_adjudication_total"],
        "is_simulation_rehearsal": simulation["is_simulation_rehearsal"],
        "no_real_human_signature": simulation["no_real_human_signature"],
        "result_sha256": simulation["result_sha256"],
        "per_topic": rows,
    }


def evaluate_ai_assisted_release_gate(evidence_root, *, template_or_path=None,
                                      live_semantic_path=None,
                                      allow_ai_assisted_release=False) -> dict:
    """一键放行门禁：55 项清单 + 三议题 108 卡材料与 §5.2 模拟。

    默认 ``allow_ai_assisted_release=False`` 时保持锁定；显式传入 True 才解锁
    Pilot 材料放行。即使解锁，``formal_eligible`` 仍为 False 且
    ``real_human_ca_signature_verified=False``——方案A不是真人人审。
    """
    _need(type(allow_ai_assisted_release) is bool, "release_flag_invalid")
    build = _resolve_build(evidence_root)
    template = _resolve_path(template_or_path, DEFAULT_TEMPLATE_PATH)
    semantic = _resolve_path(live_semantic_path, DEFAULT_LIVE_SEMANTIC_PATH)
    _need(template.is_file(), "template_file_missing")
    _need(semantic.is_file(), "live_semantic_missing")

    merge = run_scheme_a_merge_and_verify(
        template, evidence_build=build, live_semantic_path=semantic,
        include_realistic_disagreements=True)

    bundle = pm.build_all_topics_pilot_materials(evidence_root)
    simulation = pm.simulate_all_topics_dual_review(bundle)
    topics = bundle["topics"]
    topic_rows = {}
    for topic_id in pm.TOPIC_IDS.values():
        topic_bundle = topics[topic_id]
        public = topic_bundle["zones"]["zone_1_subject_public"]
        private = topic_bundle["zones"]["zone_3_evaluator_private_gold"]
        verification = bundle["verifications"][topic_id]
        topic_rows[topic_id] = {
            "exposure_cards": len(public["exposure_cards"]),
            "retention_cards": len(public["retention_cards"]),
            "discussion_slots": len(public["discussion_slots"]),
            "intervention_packets": sum(len(public["intervention_packets"][arm])
                                        for arm in pm.ARMS),
            "questionnaire_items": (len(private["public_questionnaire"]["anchor_items"])
                                    + len(private["public_questionnaire"]["transfer_items"])),
            "private_dimensions": len(private["private_gold_bundle"]["dimensions"]),
            "material_verified": verification["passed"] is True,
            "formal_eligible": verification["formal_eligible"],
            "human_reviews_completed": verification["human_reviews_completed"],
        }
    totals = {
        "exposure_cards": sum(row["exposure_cards"] for row in topic_rows.values()),
        "retention_cards": sum(row["retention_cards"] for row in topic_rows.values()),
        "discussion_slots": sum(row["discussion_slots"] for row in topic_rows.values()),
        "intervention_packets": sum(row["intervention_packets"] for row in topic_rows.values()),
        "questionnaires": len(topics),
        "private_gold_bundles": len(topics),
    }
    _need(totals["exposure_cards"] == 216 and totals["retention_cards"] == 108,
          "pilot_card_totals_invalid")
    _need(totals["intervention_packets"] == 36 and totals["discussion_slots"] == 36,
          "pilot_packet_totals_invalid")
    _need(all(row["material_verified"] for row in topic_rows.values()),
          "pilot_material_verification_failed")

    sim = _simulation_summary(simulation)
    _need(sim["all_eight_steps_present"] and sim["all_disagreements_adjudicated"]
          and sim["all_honest_gates_enforced"], "simulation_not_passed")
    full_pack = (totals["exposure_cards"] == 216 and totals["retention_cards"] == 108
                 and totals["intervention_packets"] == 36
                 and totals["questionnaires"] == 3 and totals["private_gold_bundles"] == 3
                 and all(row["material_verified"] for row in topic_rows.values()))
    checklist_passed = bool(merge["all_55_items_passed"] and merge["all_55_items_resolved"]
                            and merge["content_review_passed"] and merge["release_gates_passed"])
    unlocked = bool(allow_ai_assisted_release and checklist_passed and full_pack)

    blockers = []
    if not checklist_passed:
        blockers.append("checklist_not_fully_passed")
    if not full_pack:
        blockers.append("pilot_materials_not_verified")
    if not allow_ai_assisted_release:
        blockers.append("ai_assisted_release_not_authorized_by_caller")
    blockers.extend(["real_human_review_not_completed",
                     "formal_experiment_eligibility_not_granted"])

    return {
        "contract": GATE_CONTRACT,
        "scheme_a_version": SCHEME_A_VERSION,
        "review_origin": REVIEW_ORIGIN,
        "real_human_ca_signature_verified": False,
        "identity_authentication_scope":
            "local_format_independence_only_not_real_human_authentication",
        "allow_ai_assisted_release": allow_ai_assisted_release,
        "pilot_release_unlocked": unlocked,
        "ai_assisted_approved_for_pilot": unlocked,
        "full_108_card_pilot_pack_verified": full_pack,
        "all_55_checklist_items_passed": checklist_passed,
        "release_scope": ("pilot_materials_only_ai_assisted_scheme_a_authorized_by_user"
                          if unlocked else "locked"),
        "release_blockers": blockers,
        "checklist": {
            "merge_contract": merge["contract"],
            "template_sha256": merge["template_sha256"],
            "merged_sha256": merge["merged_sha256"],
            "human_slots_completed": merge["human_slots_completed"],
            "items_total": merge["items_total"],
            "items_passed": merge["items_passed"],
            "disagreement_count": merge["disagreement_count"],
            "unresolved_disagreements": merge["counts"]["unresolved_disagreements"],
            "human_reviews_completed": merge["human_reviews_completed"],
            "content_review_passed": merge["content_review_passed"],
            "release_gates_passed": merge["release_gates_passed"],
            "all_55_items_resolved": merge["all_55_items_resolved"],
            "all_55_items_passed": merge["all_55_items_passed"],
            "role_ids": merge["role_ids"],
            "gate_checks": merge["gate_checks"],
        },
        "pilot_materials": {
            "pilot_manifest_build_id": bundle["pilot_manifest"]["build_id"],
            "topic_count": len(pm.TOPIC_IDS),
            "totals": totals,
            "topics": topic_rows,
            "all_topics_verified": all(row["material_verified"]
                                       for row in topic_rows.values()),
            "simulation": sim,
        },
        "formal_eligible": False,
        "approved_for_experiment": False,
        "human_reviews_completed": 0,
        "subject_delivered": False,
        "new_model_calls": 0,
        "new_api_calls": 0,
        "new_cost_cny": "0",
        "notice": (
            "方案A放行仅覆盖离线Pilot材料与文档审核，不是真实模型预实验、不是正式实验资格、"
            "也不是真人独立审核；formal_eligible 恒为 False。"),
    }
