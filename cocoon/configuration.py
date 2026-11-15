"""配置审计和调用量规划。此版本故意没有付费/正式执行入口。"""
from pathlib import Path
import json
import math


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_config(config)
    return config


def validate_config(c):
    for field in ("blocks_per_topic", "agents_per_world", "exposure_rounds", "intervention_rounds", "retention_rounds"):
        if type(c.get(field)) is not int or c[field] < 1:
            raise ValueError(f"{field} 必须是正整数")
    if c["agents_per_world"] < 8 or c["agents_per_world"] % 4:
        raise ValueError("Agent数须不小于8且为4的倍数")
    if not isinstance(c.get("topics"), list) or not c["topics"] or len(set(c["topics"])) != len(c["topics"]):
        raise ValueError("议题ID列表必须非空且唯一")
    if c.get("environments") != ["A", "B", "C", "D"]:
        raise ValueError("第一阶段仅实现固定四环境")
    if c.get("intervention_environments") != ["D"]:
        raise ValueError("第一阶段仅支持预设D环境六臂，不静默改变比较")
    if c.get("arms") != [f"I{i}" for i in range(6)]:
        raise ValueError("必须保留六个干预臂")
    required = {"demo_fallback_allowed": False, "measurement_writes_to_memory": False,
                "generated_replies_reenter_primary_feed": False, "network_frozen": True,
                "cards_per_exposure_round": 6, "discussion_slots_per_round": 1,
                "max_exposure_repeats_per_card": 2, "measurement_expected_items": 8,
                "primary_endpoint": "BS_T3", "multiple_testing": "Holm"}
    for key, expected in required.items():
        if type(c.get(key)) is not type(expected) or c[key] != expected:
            raise ValueError(f"不允许改变冻结协议字段 {key}")
    if c.get("balanced_quota") != {"same": 2, "opposite": 2, "neutral": 2}:
        raise ValueError("均衡配额必须2/2/2")
    if c.get("homogeneous_quota") != {"same": 4, "opposite": 1, "neutral": 1}:
        raise ValueError("同质配额必须4/1/1")
    if c.get("confirmatory_comparisons") != ["D:I4-I0", "D:I4-I5"]:
        raise ValueError("主要比较不得更改")
    if c.get("mode") not in ("research", "engineering_dry_run"):
        raise ValueError("未知运行模式")
    return True


def call_plan(c):
    validate_config(c)
    blocks = len(c["topics"]) * c["blocks_per_topic"]
    worlds = blocks * len(c["environments"])
    branches = blocks * len(c["intervention_environments"]) * len(c["arms"])
    agents = c["agents_per_world"]
    parts = {
        "exposure_decisions": worlds * agents * c["exposure_rounds"],
        "T0_T1_measurements": worlds * agents * 2,
        "intervention_retention_decisions": branches * agents * (c["intervention_rounds"] + c["retention_rounds"]),
        "T2_T3_measurements": branches * agents * 2,
    }
    return {"blocks": blocks, "exposure_worlds": worlds, "intervention_worlds": branches,
            "base_calls": sum(parts.values()), "breakdown": parts,
            "excluded": ["助手内部模块", "镜像", "检索", "额外探针", "盲评", "重试"],
            "actual_calls": 0, "estimated_cost": None}


def readiness(c, root):
    """只报告已知状态，不凭手工布尔值声称完成运行验收。"""
    validate_config(c)
    root = Path(root)
    issues = []
    def issue(code, title, detail):
        issues.append({"code": code, "title": title, "detail": detail, "status": "blocked"})
    source = c.get("assistant", {}).get("source_path")
    if not source or not Path(source).is_file():
        issue("assistant_source", "待接入原助手源码", "请提供 cocoon-breaker-demo.zip 或服务端项目；不能用工程夹具替代四阶段助手。")
    adapter_state = None
    if source and Path(source).is_file():
        from .assistant_checks import inspect_adapter
        adapter_state = inspect_adapter(c, root)
        if adapter_state["offline_verified"]:
            issue("assistant_live", "助手适配离线通过，MiMo真实接口待验收", "MiMo传输和累计预算保护已实现；仍需本机密钥和实际探测，引用语义审核尚未完成。")
        else:
            issue("assistant_audit", "原助手适配待验收", "源码已找到；需验证原件、提示词版本、无回退执行层及离线验收日志。")
    for role in ("subject", "assistant", "evaluator"):
        model = c.get("models", {}).get(role, {})
        if any(model.get(key) is None for key in ("provider", "model", "endpoint", "temperature", "max_tokens")):
            issue(f"model_{role}", f"待配置 {role} 模型", "需服务商、模型名、完整接口、参数；凭据仅通过本机环境变量提供。")
    assistant_model = c.get("models", {}).get("assistant", {})
    if assistant_model.get("provider") == "xiaomi_mimo":
        from .mimo import credential_status
        if not credential_status()["usable_format"]:
            issue("mimo_credential", "MiMo本机密钥未配置", "只需配置MIMO_API_KEY；不会读取或写入聊天、配置文件或日志。")
    budget = c.get("budget", {})
    limit = budget.get("pilot_limit")
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
        issue("pilot_budget", "受试Agent预实验预算未确认", "已确认的¥5只覆盖首次接口与少量助手任务验证，不是受试预实验或全量实验授权。")
    intake_path = root / "experiment/review/intake_status.json"
    intake = json.loads(intake_path.read_text(encoding="utf-8")) if intake_path.is_file() else None
    evidence = c.get("evidence_candidates", {})
    candidate_note = "已有候选资料登记，须按manifest核验；" if evidence.get("build_directory") else ""
    issue("materials", "真实材料未审核冻结", candidate_note + "当前正式材料仍为0；双人审核、权利、剂量控制与金标准隔离均是独立门槛。")
    issue("oasis_integration", "OASIS 与模型调度尚未接入", "现有快照只验证本地状态容器，不含已接通的平台数据库。")
    issue("runtime_isolation", "运行时权限边界待验收", "字段白名单不等于操作系统权限隔离；受试与助手不得访问金标准和原始候选目录。")
    issue("pilot", "真实模型预实验尚未执行", "需要有效率、token、预算、方差及盲评校准实测，不能拿离线夹具替代。")
    issue("freeze", "正式执行入口保持关闭", "版本0.1仅提供工程准备和审计，不能仅改配置开启约2.5万次调用。")
    previous_runs = []
    for run_dir in sorted((root / "experiment/demo").glob("engineering_*")):
        if not run_dir.is_dir():
            continue
        failed = run_dir / "failure.json"
        summary = run_dir / "summary.json"
        if failed.is_file():
            detail = json.loads(failed.read_text(encoding="utf-8"))
            previous_runs.append({"run_id": run_dir.name, "status": "failed", "detail": detail.get("error"),
                                  "recording_method": detail.get("recording_method", "run_event"),
                                  "actual_model_calls": 0})
        elif summary.is_file():
            try:
                detail = json.loads(summary.read_text(encoding="utf-8"))
                if detail.get("status") != "passed" or detail.get("run_id") != run_dir.name:
                    raise ValueError("摘要没有匹配的成功状态")
                from .runtime import verify_events
                verified = verify_events(run_dir)
                if verified != detail.get("verification"):
                    raise ValueError("摘要与日志验证结果不匹配")
                lines = (run_dir / "events/events.jsonl").read_bytes().split(b"\n")
                final_event = json.loads(next(line for line in reversed(lines) if line))
                if final_event.get("event_type") != "engineering_run_completed" or final_event.get("status") != "success":
                    raise ValueError("缺少成功终态事件")
                previous_runs.append({"run_id": run_dir.name, "status": "passed", "detail": "离线工程验证通过", "actual_model_calls": 0})
            except (ValueError, OSError, TypeError, StopIteration):
                previous_runs.append({"run_id": run_dir.name, "status": "incomplete", "detail": "摘要损坏或完整性/终态不匹配，不能视为通过", "actual_model_calls": 0})
        else:
            previous_runs.append({"run_id": run_dir.name, "status": "incomplete", "detail": "缺少终态，不能视为完成", "actual_model_calls": 0})
    return {"ready_for_formal": False, "ready_for_paid_pilot": False,
            "implemented_stage": "assistant_adapter_offline" if adapter_state and adapter_state["offline_verified"] else "engineering_bootstrap", "blockers": issues,
            "intake": intake, "plan": call_plan(c), "previous_runs": previous_runs,
            "assistant_adapter": {key: value for key, value in adapter_state.items() if key != "summary"} if adapter_state else None}
