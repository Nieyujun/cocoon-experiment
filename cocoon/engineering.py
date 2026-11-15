"""不接网络、不模拟模型答案的端到端工程排程验证。"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import csv
import json
import random
import uuid

from .configuration import call_plan, validate_config
from .metrics import exposure_metrics
from .runtime import (EventStore, branch_state, canonical_hash, load_snapshot,
                      make_intervention_pair, measurement_view, save_snapshot,
                      utc_now, verify_events, write_json_new)
from .scheduling import (allocate, make_fixture_cards, make_network, make_population,
                         public_card, schedule_cards)


NOTICE = "合成夹具工程验证，非实验结果；未调用模型，未测量认知变化。"


def dry_run(config, project_root):
    progress = {}
    try:
        return _dry_run(config, project_root, progress)
    except Exception as exc:
        # 首次失败原样保留；禁止回退示例结果或覆盖运行目录。
        failure = {"status": "failed", "mode": "engineering_dry_run", "is_demo": True,
                   "notice": NOTICE, "error_type": type(exc).__name__, "error": str(exc),
                   "recorded_at_utc": utc_now(), "actual_model_calls": 0}
        if "store" in progress:
            progress["store"].append("engineering_run_failed", status="failed", payload=failure)
        if "out" in progress:
            write_json_new(progress["out"] / "failure.json", failure)
        raise


def _dry_run(config, project_root, progress):
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    c = deepcopy(config)
    c.update(mode="engineering_dry_run", topics=["SYNTHETIC"], blocks_per_topic=2,
             agents_per_world=8, formal_launch_enabled=False)
    validate_config(c)
    run_id = "engineering_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    parent = root / "experiment" / "demo"
    parent.mkdir(exist_ok=True)
    out = parent / run_id
    out.mkdir()
    progress["out"] = out
    (out / "snapshots").mkdir()
    write_json_new(out / "config.json", c)
    cards = make_fixture_cards()
    retention = make_fixture_cards(phase="retention")
    manifest = {"is_demo": True, "notice": NOTICE,
                "transformation_type": "synthetic", "approved_for_experiment": False,
                "cards": cards, "retention_cards": retention}
    write_json_new(out / "fixture_manifest.json", manifest)
    store = EventStore(out, c, canonical_hash(manifest), True, run_id)
    progress["store"] = store
    rows, manipulation, worlds, packet_checks = [], [], [], []
    samples = []
    total_contexts = 0
    snapshot_count = 0
    event_defaults = {"topic_id": "SYNTHETIC", "actor_role": "controller", "actor_id": "controller",
                      "arm": None, "parent_world_id": None, "target_ids": [], "attempt_id": 1,
                      "input_ref": None, "input_sha256": None, "state_before_ref": None,
                      "state_after_ref": None, "model": None, "usage": None,
                      "latency_ms": None, "error": None, "step": 0, "phase": "prepare"}
    def emit(kind, payload=None, **fields):
        merged = {**event_defaults, **fields}
        merged.setdefault("status", "prepared")
        merged.setdefault("task_id", f"{run_id}:event:{uuid.uuid4().hex}")
        merged.setdefault("parent_event_ids", [])
        return store.append(kind, payload={} if payload is None else payload, **merged)

    started = emit("engineering_run_started", {"notice": NOTICE, "planned_calls_if_real_pilot": call_plan(c)})

    def take_snapshot(state, label):
        nonlocal snapshot_count
        state_blob = store.capture(state)
        result = save_snapshot(out / "snapshots" / f"{state['world_id']}_{label}.json", state)
        if canonical_hash(load_snapshot(result["ref"])) != result["sha256"]:
            raise AssertionError("快照恢复哈希不匹配")
        snapshot_count += 1
        emit("snapshot_created", {"snapshot_path": str(Path(result["ref"]).relative_to(out)),
                                  "state_sha256": result["sha256"], "scope": "local_state_container_only",
                                  "platform_connected": False, "notice": NOTICE},
             world_id=state["world_id"], block_id=state["block_id"], seed=state["seed"],
             state_after_ref=state_blob, status="verified")
        return state_blob

    def prepare_measurement(state, point):
        before = canonical_hash(state)
        view = measurement_view(state)
        view["measurement_probe_only"] = point
        if canonical_hash(state) != before:
            raise AssertionError("只读测量污染原状态")
        emit("measurement_planned", {"timepoint": point, "subject_count": len(state["agents"]),
                                      "answers": None, "no_model_request": True},
             world_id=state["world_id"], block_id=state["block_id"], seed=state["seed"],
             phase=point, status="not_executed")

    def prepare_exposure(state, environment, phase, phase_cards, rounds, seed):
        nonlocal total_contexts
        # 预先为所有受试画像排完全部轮次，任何库存错误都在事件交付前触发。
        schedules = {agent["agent_id"]: schedule_cards(phase_cards, agent["initial_stance"], environment,
                     rounds, seed + index, c["max_exposure_repeats_per_card"])
                     for index, agent in enumerate(state["agents"])}
        candidates = store.capture({"card_ids": [card["card_id"] for card in phase_cards],
                                    "selection_rule": "frozen_joint_stratification",
                                    "material_phase": phase})
        for agent in state["agents"]:
            seen = Counter()
            delivered_stances = []
            error_count = 0
            for step, batch in enumerate(schedules[agent["agent_id"]], 1):
                public = [public_card(card) for card in batch]
                seen.update(card["card_id"] for card in batch)
                delivered_stances.extend(card["stance"] for card in batch)
                error_count += sum(not card["is_accurate"] for card in batch)
                context = {"cards": public, "discussion": {"text": "", "is_empty": True,
                           "rule": "frozen_empty_slot"}, "is_demo": True, "notice": NOTICE}
                captured = store.capture(context)
                selection = emit("candidate_selected", {"selected_ids": [card["card_id"] for card in batch],
                                 "candidate_pool": candidates, "selection_reason": "frozen_quota", "rank_score": None},
                                 world_id=state["world_id"], block_id=state["block_id"], seed=state["seed"],
                                 actor_id=agent["agent_id"], phase=phase, step=step)
                prepared = emit("exposure_prepared", {"cards": [card["card_id"] for card in batch],
                                "order": [card["card_id"] for card in batch],
                                "repeat_counts": {card["card_id"]: seen[card["card_id"]] for card in batch},
                                "truncated": False, "discussion_slots": 1, "sent_to_model": False},
                                world_id=state["world_id"], block_id=state["block_id"], seed=state["seed"],
                                actor_id=agent["agent_id"], phase=phase, step=step,
                                input_ref=captured["ref"], input_sha256=captured["sha256"],
                                parent_event_ids=[selection["event_id"]])
                state["prepared_inputs"].append({"agent_id": agent["agent_id"], "phase": phase,
                                                "round": step, "context": captured})
                if len(samples) < 12:
                    samples.append({"event_id": prepared["event_id"], "world_id": state["world_id"],
                                    "logical_seq": prepared["logical_seq"], "agent_id": agent["agent_id"],
                                    "input_sha256": captured["sha256"], "context": context})
                total_contexts += 1
            metrics = exposure_metrics(delivered_stances, agent["initial_stance"])
            manipulation.append({"world_id": state["world_id"], "block_id": state["block_id"],
                                 "agent_id": agent["agent_id"], "environment": environment, "phase": phase,
                                 "prepared_cards": len(delivered_stances), "Hn": metrics["Hn"], "X": metrics["X"],
                                 "error_ratio": error_count / len(delivered_stances), "max_repeat": max(seen.values()),
                                 "basis": "prepared_not_delivered"})
        state["prepared_phase"] = phase
        state["prepared_rounds"] = rounds

    for block_index in range(c["blocks_per_topic"]):
        seed = c["master_seed"] + block_index
        block = f"SYNTHETIC_B{block_index + 1:02d}"
        population = make_population(c["agents_per_world"], seed)
        network = make_network(population, seed)
        slots = allocate(c["arms"], seed)
        for slot in slots:
            rows.append({"topic_id": "SYNTHETIC", "block_id": block, "seed": seed, **slot})
        baseline = {"world_id": block + "_BASE", "block_id": block, "seed": seed,
                    "system_prompt": "仅工程状态容器；没有受试Agent执行。", "agents": population,
                    "messages": {agent["agent_id"]: [] for agent in population}, "network": network,
                    "platform_database": None, "platform_integration": "not_connected",
                    "prepared_inputs": [], "prepared_phase": None, "prepared_rounds": 0,
                    "natural_views": {}, "memory_summaries": {}, "truncation_records": [],
                    "queues": [], "recommendation_buffer": [], "pending_tasks": [],
                    "random_state": json.loads(json.dumps(random.Random(seed).getstate())),
                    "model_config": c["models"], "config_hash": canonical_hash(c),
                    "material_manifest_hash": canonical_hash(manifest), "is_demo": True}
        base_hash = canonical_hash({"agents": population, "network": network})
        d_state = None
        for environment in c["environments"]:
            state = branch_state(baseline, f"{block}_{environment}")
            created = emit("world_created", {"environment": environment, "base_population_network_hash": base_hash,
                                            "notice": NOTICE}, world_id=state["world_id"], block_id=block, seed=seed,
                           parent_event_ids=[started["event_id"]])
            worlds.append({"world_id": state["world_id"], "block_id": block, "environment": environment,
                           "arm": None, "status": "prepared_only", "base_hash": base_hash,
                           "created_event_id": created["event_id"]})
            take_snapshot(state, "T0_PREPARED")
            prepare_measurement(state, "T0")
            prepare_exposure(state, environment, "exposure", cards, c["exposure_rounds"], seed)
            take_snapshot(state, "T1_PREPARED")
            prepare_measurement(state, "T1")
            if environment == "D":
                d_state = state
        snapshot_hash = canonical_hash(d_state)
        # 证据包在分支前一次冻结。仅工程文字，不声称为原助手报告。
        pairs = {}
        for agent in population:
            for turn in range(c["intervention_rounds"]):
                packet = {"packet_id": f"{block}_{agent['agent_id']}_{turn + 1}", "evidence": [
                    {"evidence_id": f"E{turn + 1}", "text": "【合成夹具】纸环沙盒每班四个座位、两班共八个座位；此为虚构规则。",
                     "source_url": "synthetic://fixture/rules", "source_family_id": "fixture_rules"}]}
                text = "【仅用于等长契约测试，不是助手输出】请根据已列出的虚构规则核对文字，不引入外部事实。" * 14
                pair = make_intervention_pair(packet, text, text,
                        min_chars=c["intervention_min_chars"], max_chars=c["intervention_max_chars"],
                        tolerance=c["intervention_length_tolerance"])
                # 夹具长度由已知规则构造；不截断模型输出或伪造语义审计。
                if not pair["checks"]["dose_within_range"]:
                    raise AssertionError("工程证据包长度不满足契约")
                pairs[agent["agent_id"], turn] = pair
                packet_checks.append({"packet_id": packet["packet_id"], **pair["checks"]})
        children = [branch_state(d_state, f"{block}_D_{slot['arm']}") for slot in slots]
        if canonical_hash(d_state) != snapshot_hash:
            raise AssertionError("分支改变父状态")
        for slot, child in zip(slots, children):
            arm = slot["arm"]
            child["response_random_state"] = json.loads(json.dumps(random.Random(slot["response_seed"]).getstate()))
            child_hash = canonical_hash({"messages": child["messages"], "agents": child["agents"], "prepared_inputs": child["prepared_inputs"]})
            parent_hash = canonical_hash({"messages": d_state["messages"], "agents": d_state["agents"], "prepared_inputs": d_state["prepared_inputs"]})
            if child_hash != parent_hash:
                raise AssertionError("分支处理前状态不匹配")
            emit("branch_created", {"arm": arm, "slot": slot["slot"], "response_seed": slot["response_seed"],
                                    "pre_treatment_state_hash": child_hash, "parent_state_hash": snapshot_hash},
                 world_id=child["world_id"], parent_world_id=d_state["world_id"], block_id=block, seed=seed, arm=arm)
            worlds.append({"world_id": child["world_id"], "block_id": block, "environment": "D", "arm": arm,
                           "status": "prepared_only", "base_hash": base_hash})
            take_snapshot(child, "BRANCH_PREPARED")
            for turn in range(c["intervention_rounds"]):
                for agent in population:
                    if arm in ("I4", "I5"):
                        package = pairs[agent["agent_id"], turn][arm]
                        package = {**package, "semantic_review": "pending", "notice": NOTICE,
                                   "original_assistant_connected": False, "sent_to_model": False}
                        kind = "intervention_prepared"
                    else:
                        package = {"arm": arm, "notice": NOTICE, "visible_text": None,
                                   "reason": "此臂真实干预材料尚待审核，不以替代文本充数。"}
                        kind = "intervention_planned"
                    emit(kind, package, world_id=child["world_id"], block_id=block, seed=seed,
                         actor_id=agent["agent_id"], phase="intervention", step=turn + 1, arm=arm,
                         status="prepared" if arm in ("I4", "I5") else "not_executed")
            prepare_measurement(child, "T2")
            prepare_exposure(child, "D", "retention", retention, c["retention_rounds"], seed + 1000)
            prepare_measurement(child, "T3")
            take_snapshot(child, "T3_PREPARED")
        # 只在隔离测试副本写入标记；不会污染任何已保存状态。
        children[0]["messages"][population[0]["agent_id"]].append("isolation_probe")
        if any(child["messages"][population[0]["agent_id"]] for child in children[1:]):
            raise AssertionError("分支共享可变状态")
        if canonical_hash(d_state) != snapshot_hash:
            raise AssertionError("隔离测试污染父状态")

    with (out / "allocation.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    emit("engineering_run_completed", {"notice": NOTICE, "model_calls": 0, "formal_observations": 0}, status="success")
    verification = verify_events(out)
    summary = {"schema_version": "1.0", "created_at_utc": utc_now(), "run_id": run_id,
               "run_directory": str(out), "notice": NOTICE, "is_demo": True,
               "mode": "engineering_dry_run", "status": "passed", "actual_model_calls": 0,
               "actual_cost": 0, "currency": "CNY", "formal_observations": 0,
               "prepared_contexts": total_contexts, "snapshot_count": snapshot_count,
               "plan": call_plan(c), "verification": verification, "worlds": worlds,
               "manipulation": manipulation, "packet_checks": packet_checks,
               "trace_samples": samples, "allocation_rows": len(rows),
               "not_implemented": ["真实模型调度及重试", "OASIS平台接入", "原助手四阶段及镜像接入",
                                   "真实材料双人核验", "操作系统金标准权限隔离", "正式随机化检验"]}
    write_json_new(out / "summary.json", summary)
    return summary
