"""步骤32：网络级分析层专项测试。全部为显式合成内存夹具，不落盘、不联网、不调用模型。

覆盖：个体长面板→网络聚合（缺测固定分母、8锚/4迁移隔离）、2×2曝光对比、
六臂配对干预效应、实际 scheduling.allocate 槽位分配的区组内置换检验与 Holm 校正，
以及从 measurement.reconcile_blind_evaluations 到 analysis 的离线闭环。
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock

from cocoon import analysis, measurement as m, metrics, runtime, scheduling

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from test_measurement import (  # noqa: E402  纯内存合成夹具，不执行其测试类
    dumps, fixture_blind_output, fixture_gold, fixture_output, fixture_questionnaire,
    registry_evaluations, registry_measurements,
)

CROSS = ROOT / "experiment/review/cross_topic_20261108"
MEASUREMENT_REVIEW = ROOT / "experiment/review/measurement_20261108"
PROTECTED = [CROSS / name for name in (
    "feasibility.json", "human_review_checklist.json", "independent_review.json",
    "review.html", "delivery.json", "REPORT_step28_20261108.md", "REPORT_step29_20261108.md")]
PROTECTED += [MEASUREMENT_REVIEW / name for name in ("REPORT_step30_20261108.md", "REPORT_step31_20261108.md")]
PROTECTED += [ROOT / "experiment/billing/mimo_initial_probe_v1.jsonl", ROOT / "破茧助手全量实验方案_v1.md"]
PROTECTED += [ROOT / "cocoon" / name for name in (
    "metrics.py", "runtime.py", "contracts.py", "scheduling.py", "human_review.py",
    "measurement.py", "extractive_control.py", "reporting.py")]

ARMS = ("I0", "I1", "I2", "I3", "I4", "I5")
ENVS = ("A", "B", "C", "D")


def dumps_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _metric_set(bs):
    return {"BS": bs, "Acc": 100 * (1 - bs), "S": 100 * bs, "O": 100 * bs, "U": 100 * (1 - bs)}


def _row(*, sample, topic="TOPIC_A", block="B01", world="W01", env="D", arm=None,
         timepoint="T1", agent=1, bs=0.2, objective=True, blind=True, demo=True,
         transfer=True):
    values = _metric_set(bs)
    anchor = {"complete": objective, "expected_n": 8,
              "BS": values["BS"] if objective else None,
              "Acc": values["Acc"] if objective else None,
              "S": values["S"] if objective else None,
              "BS_lower": bs, "BS_upper": bs}
    transfer_summary = None if not transfer else {
        "complete": objective, "expected_n": 4,
        "BS": bs if objective else None,
        "Acc": values["Acc"] if objective else None,
        "S": values["S"] if objective else None,
        "BS_lower": bs, "BS_upper": bs}
    complete = objective and blind
    return {
        "blind_sample_id": sample, "world_id": world, "agent_id": f"S{agent:02d}",
        "environment": env, "arm": arm, "timepoint": timepoint, "block_id": block,
        "topic_id": topic, "questionnaire_id": "QSET01", "is_demo": demo,
        "BS": values["BS"] if objective else None,
        "Acc": values["Acc"] if objective else None,
        "S": values["S"] if objective else None,
        "BS_lower": bs, "BS_upper": bs,
        "O": values["O"] if complete else None, "U": values["U"] if complete else None,
        "blind_evaluation_status": "validated" if complete else "pending",
        "objective_complete": objective, "row_complete": complete,
        "anchor": anchor, "transfer": transfer_summary,
        "primary_endpoint_partition": "anchor_eight_only", "transfer_scope": "exploratory_only",
        "controller_private_only": True, "actual_observation_verified": False,
        "formal_eligible": False,
    }


class PanelBuilder:
    """按 (议题,区组,世界,环境,臂,时点) 生成8名个体的合成面板，自动分配唯一匿名ID。"""

    def __init__(self, *, demo=True):
        self.rows = []
        self.demo = demo
        self.serial = 0

    def add(self, *, n=8, bs=0.2, **kwargs):
        values = bs if isinstance(bs, (list, tuple)) else [bs] * n
        if len(values) != n:
            raise AssertionError("每条网络记录必须给 n 个 BS 值")
        for index, value in enumerate(values, 1):
            self.serial += 1
            self.rows.append(_row(sample=f"SID_{self.serial:05d}", agent=index, bs=value,
                                  demo=self.demo, **kwargs))
        return self

    def panel(self, *, demo=None):
        rows = deepcopy(self.rows)
        demo = self.demo if demo is None else demo
        completed = sum(bool(row["row_complete"]) for row in rows)
        pending = [row["blind_sample_id"] for row in rows if not row["row_complete"]]
        panel = {
            "contract": "reconciled_blind_panel_v1", "registry_sha256": "0" * 64,
            "panel_records": rows, "pending_blind_sample_ids": pending,
            "expected_samples": len(rows), "completed_blind_evaluations": completed,
            "pending_blind_evaluations": len(pending),
            "complete_objective_records": sum(bool(row["objective_complete"]) for row in rows),
            "complete_panel_records": completed,
            "status": "all_blind_evaluations_reconciled" if not pending else "partial_blind_evaluations",
            "controller_private_only": True, "send_to_evaluator": False, "writes_to_memory": False,
            "new_model_calls": 0, "is_demo": demo, "formal_eligible": False,
            "analysis_scope": "individual_long_panel_not_network_aggregate_or_formal_effect_estimate",
        }
        panel["panel_sha256"] = runtime.canonical_hash(rows)
        return panel


def exposure_panel(*, demo=True, changes=None, omit_t1=None, add_armed_t1=False,
                   topics=("TOPIC_A", "TOPIC_B"), blocks=("B01", "B02"), base=0.30):
    """四环境×T0/T1，每个网络8人；changes 为 T1-T0 的目标变化量。"""
    changes = changes or {"A": 0.10, "B": 0.20, "C": 0.30, "D": 0.40}
    omit_t1 = omit_t1 or set()
    builder = PanelBuilder(demo=demo)
    for topic in topics:
        for block in blocks:
            for env in ENVS:
                builder.add(topic=topic, block=block, world=f"W_{topic[-1]}_{block}_{env}",
                            env=env, arm=None, timepoint="T0", bs=base)
                if (topic, block, env) not in omit_t1:
                    builder.add(topic=topic, block=block, world=f"W_{topic[-1]}_{block}_{env}",
                                env=env, arm=None, timepoint="T1", bs=base + changes[env])
                if add_armed_t1 and env == "D":
                    for arm in ARMS:
                        builder.add(topic=topic, block=block, world=f"W_{topic[-1]}_{block}_{arm}",
                                    env=env, arm=arm, timepoint="T1", bs=0.50)
    return builder.panel()


def intervention_panel(*, demo=True, topics=("TOPIC_A",), blocks=("B01", "B02"),
                       endpoint="T3", baseline=0.50, ends=None, t1_overrides=None,
                       incomplete_block=None):
    """环境D六臂，T1共享起点，终点为 endpoint；每臂一个独立世界。"""
    ends = ends or {"I0": 0.60, "I1": 0.55, "I2": 0.50, "I3": 0.45, "I4": 0.20, "I5": 0.55}
    t1_overrides = t1_overrides or {}
    builder = PanelBuilder(demo=demo)
    for topic in topics:
        for block in blocks:
            for arm in ARMS:
                world = f"W_{topic[-1]}_{block}_{arm}"
                broken = (topic, block) == incomplete_block
                builder.add(topic=topic, block=block, world=world, env="D", arm=arm,
                            timepoint="T1", bs=t1_overrides.get(arm, baseline))
                builder.add(topic=topic, block=block, world=world, env="D", arm=arm,
                            timepoint=endpoint, bs=ends[arm],
                            objective=not broken, blind=not broken)
    return builder.panel()


def allocation_for(*, topics=("TOPIC_A",), blocks=("B01", "B02"), seeds=None):
    seeds = seeds or {}
    return [{"topic_id": topic, "block_id": block,
             "seed": seeds.get((topic, block), 100 + 7 * i + 3 * j),
             "rows": scheduling.allocate(list(ARMS), seeds.get((topic, block), 100 + 7 * i + 3 * j))}
            for i, topic in enumerate(topics) for j, block in enumerate(blocks)]


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in PROTECTED}
        cls.directory_before = {d: set(d.iterdir()) for d in (CROSS, MEASUREMENT_REVIEW)}

    @classmethod
    def tearDownClass(cls):
        if cls.protected_before != {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in cls.protected_before}:
            raise AssertionError("步骤28-31受保护原文件被改写")
        if cls.directory_before != {d: set(d.iterdir()) for d in cls.directory_before}:
            raise AssertionError("测试不得向旧审阅目录写入文件")

    def setUp(self):
        patcher = mock.patch.object(socket, "socket", side_effect=AssertionError("禁止网络请求"))
        self.network = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.network.assert_not_called)
        real_open = Path.open

        def read_only(path, mode="r", *args, **kwargs):
            if any(letter in mode for letter in "wax+"):
                raise AssertionError("禁止落盘合成金标准或分析结果")
            return real_open(path, mode, *args, **kwargs)

        patcher = mock.patch.object(Path, "open", read_only)
        patcher.start()
        self.addCleanup(patcher.stop)

    def reject(self, function, *args, **kwargs):
        with self.assertRaisesRegex(ValueError, "^analysis:"):
            function(*args, **kwargs)

    def aggregate(self, panel, **kwargs):
        return analysis.aggregate_panel_to_networks(panel, allow_demo=True, **kwargs)

    # ---------- 1. 个体面板 → 网络聚合 ----------
    def test_01_complete_network_exact_mean_and_partition(self):
        values = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
        panel = PanelBuilder().add(bs=values).panel()
        before = deepcopy(panel)
        result = self.aggregate(panel, expected_agents_per_world=8)
        self.assertEqual(result["contract"], "network_aggregation_v1")
        self.assertEqual(len(result["network_records"]), 1)
        row = result["network_records"][0]
        self.assertTrue(row["valid"] and row["blind_valid"] and row["all_metrics_valid"])
        self.assertEqual((row["expected_agents"], row["observed_agents"], row["missing_agents"]), (8, 8, 0))
        self.assertAlmostEqual(row["BS"], math.fsum(values) / 8)
        self.assertAlmostEqual(row["BS_lower_mean"], math.fsum(values) / 8)
        self.assertAlmostEqual(row["BS_upper_mean"], math.fsum(values) / 8)
        self.assertEqual(row["transfer"]["scope"], "exploratory_only")
        self.assertTrue(row["transfer"]["primary_endpoint_separate"])
        self.assertEqual(row["primary_endpoint_partition"], "anchor_eight_only")
        self.assertEqual(row["independent_unit"], "network_world_timepoint")
        self.assertFalse(result["formal_eligible"])
        self.assertTrue(result["is_demo"])
        self.assertEqual(panel, before)

    def test_02_missing_one_agent_fixed_denominator_no_silent_drop(self):
        values = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70]
        result = self.aggregate(PanelBuilder().add(n=7, bs=values).panel(), expected_agents_per_world=8)
        row = result["network_records"][0]
        self.assertFalse(row["valid"])
        self.assertIsNone(row["BS"])
        self.assertIsNone(row["Acc"])
        self.assertIsNone(row["S"])
        self.assertEqual(row["missing_agents"], 1)
        self.assertAlmostEqual(row["BS_lower_mean"], math.fsum(values) / 8)
        self.assertAlmostEqual(row["BS_upper_mean"], (math.fsum(values) + 1.0) / 8)
        self.assertAlmostEqual(row["transfer"]["BS_lower_mean"], math.fsum(values) / 8)
        self.assertAlmostEqual(row["transfer"]["BS_upper_mean"], (math.fsum(values) + 1.0) / 8)

    def test_03_incomplete_objective_row_invalidates_whole_network(self):
        rows = [_row(sample=f"SID_{i:05d}", agent=i, bs=0.10 * i) for i in range(1, 9)]
        rows[3]["objective_complete"] = False
        rows[3]["row_complete"] = False
        rows[3].update({"BS": None, "Acc": None, "S": None, "O": None, "U": None})
        rows[3]["anchor"] = {"complete": False, "expected_n": 8, "BS": None, "Acc": None,
                             "S": None, "BS_lower": 0.40, "BS_upper": 0.40}
        rows[3]["transfer"] = {"complete": False, "expected_n": 4, "BS": None, "Acc": None,
                               "S": None, "BS_lower": 0.40, "BS_upper": 0.40}
        rows[3]["blind_evaluation_status"] = "pending"
        panel = PanelBuilder().panel()  # 仅取模板结构
        panel["panel_records"] = rows
        panel["expected_samples"] = 8
        panel["completed_blind_evaluations"] = 7
        panel["pending_blind_evaluations"] = 1
        panel["pending_blind_sample_ids"] = ["SID_00004"]
        panel["complete_objective_records"] = 7
        panel["complete_panel_records"] = 7
        panel["status"] = "partial_blind_evaluations"
        panel["panel_sha256"] = runtime.canonical_hash(rows)
        row = self.aggregate(panel, expected_agents_per_world=8)["network_records"][0]
        self.assertFalse(row["valid"])
        self.assertIsNone(row["BS"])
        self.assertEqual(row["objective_complete_count"], 7)
        self.assertAlmostEqual(row["BS_lower_mean"], math.fsum(0.10 * i for i in range(1, 9)) / 8)

    def test_04_population_size_and_overfilled_network_rejected(self):
        panel = PanelBuilder().add(n=8).panel()
        for value in (7, 9, 8.0, True, 0, -8, "8"):
            self.reject(analysis.aggregate_panel_to_networks, panel,
                        expected_agents_per_world=value, allow_demo=True)
        for value in (8, 12, 16):
            result = self.aggregate(PanelBuilder().add(n=8).panel(), expected_agents_per_world=value)
            self.assertEqual(result["expected_agents_per_world"], value)
        self.reject(analysis.aggregate_panel_to_networks, PanelBuilder().add(n=9).panel(),
                    expected_agents_per_world=8, allow_demo=True)

    def test_05_demo_gate_and_non_demo_path(self):
        demo = PanelBuilder(demo=True).add(n=8).panel()
        self.reject(analysis.aggregate_panel_to_networks, demo, expected_agents_per_world=8)
        explicit = self.aggregate(demo, expected_agents_per_world=8)
        self.assertTrue(explicit["is_demo"])
        self.assertFalse(explicit["formal_eligible"])
        real = PanelBuilder(demo=False).add(n=8).panel()
        result = analysis.aggregate_panel_to_networks(real, expected_agents_per_world=8)
        self.assertFalse(result["is_demo"])
        self.assertFalse(result["allow_demo"])

    def test_06_panel_tampering_rejected(self):
        good = PanelBuilder().add(n=8).panel()
        for mutate in (
            lambda p: p.update(panel_sha256="0" * 64),
            lambda p: p.update(contract="other"),
            lambda p: p.update(formal_eligible=True),
            lambda p: p.update(new_model_calls=1),
            lambda p: p.update(is_demo=False),
            lambda p: p["panel_records"].append(deepcopy(p["panel_records"][0])),
            lambda p: p["panel_records"].__setitem__(1, {**p["panel_records"][1], "agent_id": "S01"}),
        ):
            panel = deepcopy(good)
            mutate(panel)
            self.reject(analysis.aggregate_panel_to_networks, panel,
                        expected_agents_per_world=8, allow_demo=True)

    # ---------- 2. 2×2 曝光效应 ----------
    def test_07_exposure_2x2_hand_calculation_matches_metrics(self):
        panel = exposure_panel()
        aggregation = self.aggregate(panel, expected_agents_per_world=8)
        result = analysis.analyze_exposure_2x2(aggregation, target_topics=["TOPIC_A", "TOPIC_B"],
                                               metric="BS", allow_demo=True)
        expected_changes = {"A": 0.10, "B": 0.20, "C": 0.30, "D": 0.40}
        contrasts = metrics.exposure_contrasts(expected_changes)
        block = result["per_block"][0]
        self.assertTrue(block["valid"])
        for env, value in expected_changes.items():
            self.assertAlmostEqual(block["changes"][env], value)
        for key, value in contrasts.items():
            self.assertAlmostEqual(block["contrasts"][key], value)
        self.assertAlmostEqual(contrasts["homogeneous"], 0.20)
        self.assertAlmostEqual(contrasts["misinformation"], 0.10)
        self.assertAlmostEqual(contrasts["interaction"], 0.0)
        self.assertTrue(result["overall"]["estimable"])
        for key, value in contrasts.items():
            self.assertAlmostEqual(result["overall"]["contrasts"][key], value)
        self.assertGreaterEqual(result["overall"]["bounds_from_missingness"]["homogeneous"][0],
                                result["per_block"][0]["bounds_from_missingness"]["homogeneous"][0])
        self.assertEqual(result["exposure_arm_scope"], "unassigned_pre_intervention_networks_only")
        self.assertEqual(result["underlying_operator"], "metrics.exposure_contrasts")

    def test_08_missing_t1_cell_invalidates_block_and_topic(self):
        panel = exposure_panel(omit_t1={("TOPIC_A", "B01", "C")})
        aggregation = self.aggregate(panel, expected_agents_per_world=8)
        result = analysis.analyze_exposure_2x2(aggregation, target_topics=["TOPIC_A"],
                                               allow_demo=True)
        broken = [b for b in result["per_block"] if b["block_id"] == "B01"]
        self.assertFalse(broken[0]["valid"])
        self.assertIsNone(broken[0]["changes"])
        self.assertIsNone(broken[0]["contrasts"])
        self.assertEqual(result["per_topic"]["TOPIC_A"]["complete_blocks"], 1)
        self.assertEqual(result["per_topic"]["TOPIC_A"]["missing_blocks"], ["B01"])
        self.assertAlmostEqual(result["per_topic"]["TOPIC_A"]["coverage"], 0.5)

    def test_09_armed_t1_rows_do_not_pollute_exposure_networks(self):
        plain = analysis.analyze_exposure_2x2(
            self.aggregate(exposure_panel(), expected_agents_per_world=8),
            target_topics=["TOPIC_A"], allow_demo=True)
        mixed = analysis.analyze_exposure_2x2(
            self.aggregate(exposure_panel(add_armed_t1=True), expected_agents_per_world=8),
            target_topics=["TOPIC_A"], allow_demo=True)
        self.assertEqual(mixed["exposure_arm_scope"], "unassigned_pre_intervention_networks_only")
        self.assertEqual(mixed["overall"]["contrasts"], plain["overall"]["contrasts"])
        self.assertEqual(len(mixed["per_block"]), len(plain["per_block"]))

    def test_10_non_BS_metric_has_no_BS_missingness_bounds(self):
        aggregation = self.aggregate(exposure_panel(), expected_agents_per_world=8)
        result = analysis.analyze_exposure_2x2(aggregation, target_topics=["TOPIC_A"],
                                               metric="Acc", allow_demo=True)
        self.assertTrue(result["higher_is_better"] if "higher_is_better" in result else
                        "higher_is_better" in result["metric_direction"])
        self.assertIsNone(result["per_block"][0]["bounds_from_missingness"])
        self.assertEqual(result["bounds_scope"], "not_applicable_non_BS_metric")

    def test_11_partial_blind_evaluation_keeps_subjective_metrics_none(self):
        builder = PanelBuilder()
        for i in range(1, 9):
            builder.rows.append(_row(sample=f"SID_{i:05d}", agent=i, bs=0.2,
                                     objective=True, blind=False))
        panel = builder.panel()
        panel.update({"completed_blind_evaluations": 0, "pending_blind_evaluations": 8,
                      "pending_blind_sample_ids": [f"SID_{i:05d}" for i in range(1, 9)],
                      "complete_panel_records": 0, "status": "partial_blind_evaluations"})
        row = self.aggregate(panel, expected_agents_per_world=8)["network_records"][0]
        self.assertTrue(row["valid"])
        self.assertFalse(row["blind_valid"])
        self.assertIsNone(row["O"])
        self.assertIsNone(row["U"])

    # ---------- 3. 配对干预效应与随机化检验 ----------
    def test_12_shared_T1_mismatch_rejected(self):
        panel = intervention_panel(t1_overrides={"I4": 0.51})
        aggregation = self.aggregate(panel, expected_agents_per_world=8)
        self.reject(analysis.analyze_intervention_effects, aggregation,
                    target_topics=["TOPIC_A"], allow_demo=True, bootstrap_reps=20,
                    permutation_reps=20)

    def test_13_without_allocations_p_values_stay_none(self):
        aggregation = self.aggregate(intervention_panel(), expected_agents_per_world=8)
        result = analysis.analyze_intervention_effects(
            aggregation, target_topics=["TOPIC_A"], allow_demo=True,
            bootstrap_reps=30, permutation_reps=30)
        self.assertIsNone(result["p_raw"]["I4_vs_I0"])
        self.assertIsNone(result["p_raw"]["I4_vs_I5"])
        self.assertIsNone(result["p_adjusted"]["I4_vs_I0"])
        self.assertIn("allocation", result["p_value_note"])
        for name, item in result["comparisons"].items():
            self.assertEqual(item["comparison_family"], "primary_confirmatory")
            self.assertEqual(item["p_value_status"], "not_computed")
            self.assertIn(name, result["coverage"])
        self.assertEqual(set(result["secondary_comparisons"]),
                         {"I1_vs_I0", "I2_vs_I0", "I3_vs_I0", "I5_vs_I0"})
        for item in result["secondary_comparisons"].values():
            self.assertEqual(item["comparison_family"], "secondary_exploratory")
            self.assertIsNone(item["p_raw"])
            self.assertIsNone(item["p_adjusted"])
        self.assertAlmostEqual(result["comparisons"]["I4_vs_I0"]["effect"], 0.40)
        self.assertAlmostEqual(result["comparisons"]["I4_vs_I5"]["effect"], 0.35)
        self.assertEqual(result["coverage"]["I4_vs_I0"]["n"], 2)
        self.assertEqual(result["coverage"]["I4_vs_I0"]["count_missing"], 0)
        self.assertAlmostEqual(result["coverage"]["I4_vs_I0"]["coverage"], 1.0)

    def test_14_real_allocations_produce_randomization_and_holm_p_values(self):
        aggregation = self.aggregate(intervention_panel(), expected_agents_per_world=8)
        allocations = allocation_for(seeds={("TOPIC_A", "B01"): 41, ("TOPIC_A", "B02"): 42})
        before = deepcopy(allocations)
        kwargs = {"target_topics": ["TOPIC_A"], "allocations": allocations,
                  "allow_demo": True, "bootstrap_reps": 40, "permutation_reps": 200, "seed": 7}
        result = analysis.analyze_intervention_effects(aggregation, **kwargs)
        again = analysis.analyze_intervention_effects(aggregation, **kwargs)
        self.assertEqual(result["p_raw"], again["p_raw"])
        self.assertEqual(result["p_adjusted"], again["p_adjusted"])
        self.assertEqual(allocations, before)
        for value in result["p_raw"].values():
            self.assertIsInstance(value, float)
            self.assertGreater(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertEqual(result["p_adjusted"], metrics.holm(dict(result["p_raw"])))
        self.assertEqual(result["randomization"]["holm_operator"], "metrics.holm")
        self.assertEqual(result["randomization"]["permutation_reps"], 200)
        self.assertEqual(result["randomization"]["complete_blocks"], 2)
        self.assertIn("(1+count", result["randomization"]["p_formula"])
        self.assertTrue(result["confirmatory_records_restricted_to_complete_allocation_blocks"])
        self.assertEqual(result["allocation_validation"]["registered_blocks"], 2)
        self.assertEqual(result["allocation_validation"]["complete_blocks"], 2)
        self.assertTrue(result["is_demo"])
        self.assertFalse(result["formal_eligible"])

    def test_15_allocation_tampering_and_cross_block_rejection(self):
        aggregation = self.aggregate(intervention_panel(), expected_agents_per_world=8)
        base = dict(target_topics=["TOPIC_A"], allow_demo=True,
                    bootstrap_reps=20, permutation_reps=20)

        def check(mutate):
            allocations = deepcopy(allocation_for())
            mutate(allocations)
            self.reject(analysis.analyze_intervention_effects, aggregation,
                        allocations=allocations, **base)

        check(lambda a: a[0]["rows"][0].update(response_seed=a[0]["rows"][0]["response_seed"] + 1))
        check(lambda a: a[0]["rows"][0].update(arm="I5"))
        check(lambda a: a[0]["rows"][0].update(slot=99))
        check(lambda a: a[0].update(seed=a[0]["seed"] + 999))
        check(lambda a: a.pop())
        check(lambda a: a.append(deepcopy(a[0])))
        check(lambda a: a[0].update(topic_id="UNREGISTERED"))
        check(lambda a: a[0]["rows"].append(dict(a[0]["rows"][0])))

    def test_16_incomplete_block_kept_in_denominator_and_p_values_none(self):
        panel = intervention_panel(incomplete_block=("TOPIC_A", "B02"))
        aggregation = self.aggregate(panel, expected_agents_per_world=8)
        allocations = allocation_for()
        result = analysis.analyze_intervention_effects(
            aggregation, target_topics=["TOPIC_A"], allocations=allocations,
            allow_demo=True, bootstrap_reps=20, permutation_reps=20)
        self.assertEqual(result["coverage"]["I4_vs_I0"]["n"], 1)
        self.assertEqual(result["coverage"]["I4_vs_I0"]["count_missing"], 1)
        self.assertAlmostEqual(result["coverage"]["I4_vs_I0"]["coverage"], 0.5)
        # 只在1个完整有效区组内重排；不是伪造p值，但必须显式标为条件推断。
        self.assertEqual(result["randomization_scope"], "conditional_on_complete_blocks_only")
        self.assertEqual(result["randomization"]["complete_blocks"], 1)
        self.assertEqual(result["allocation_validation"]["complete_blocks"], 1)
        for value in result["p_raw"].values():
            self.assertGreater(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertEqual(result["p_adjusted"], metrics.holm(dict(result["p_raw"])))

    def test_17_endpoint_T2_and_non_BS_metric_direction(self):
        aggregation = self.aggregate(intervention_panel(endpoint="T2"), expected_agents_per_world=8)
        result = analysis.analyze_intervention_effects(
            aggregation, target_topics=["TOPIC_A"], endpoint_timepoint="T2",
            metric="Acc", allow_demo=True, bootstrap_reps=20, permutation_reps=20)
        self.assertEqual(result["endpoint_timepoint"], "T2")
        self.assertTrue(result["higher_is_better"])
        self.assertEqual(result["metric_scale"], 100.0)
        self.assertEqual(result["bounds_from_missingness"]["scope"], "not_applicable_non_BS_metric")
        self.assertIn("positive_means_treatment_improved_more", result["effect_definition"])

    def test_18_allocation_block_set_and_arm_set_must_match_network(self):
        aggregation = self.aggregate(intervention_panel(), expected_agents_per_world=8)
        self.reject(analysis.analyze_intervention_effects, aggregation,
                    target_topics=["TOPIC_A"], allocations=None,
                    endpoint_timepoint="T9", allow_demo=True)
        self.reject(analysis.analyze_intervention_effects, aggregation,
                    target_topics=["NOT_PRESENT"], allow_demo=True,
                    bootstrap_reps=20, permutation_reps=20)
        self.reject(analysis.analyze_intervention_effects, aggregation,
                    target_topics=["TOPIC_A"], allow_demo=True,
                    bootstrap_reps=0, permutation_reps=20)
        self.reject(analysis.analyze_intervention_effects, aggregation,
                    target_topics=["TOPIC_A"], allow_demo=True,
                    bootstrap_reps=20, permutation_reps=0)

    # ---------- 4. 从 reconcile_blind_evaluations 的端到端闭环 ----------
    def test_19_reconciled_panel_to_network_to_exposure_end_to_end(self):
        q = fixture_questionnaire()
        private = m.validate_private_gold_bundle(fixture_gold(q), q)
        bs_by_env = {"A": 0.0, "B": 0.125, "C": 0.0, "D": 0.25}
        entries = []
        for env in ENVS:
            for point in ("T0", "T1"):
                p_true = 0.0 if point == "T0" else (1.0 if bs_by_env[env] >= 0.125 else 0.0)
                for agent in range(1, 9):
                    output = fixture_output(q)
                    # 每题真值：锚题为0、迁移题为1；只调第一题，形成可复算的BS差异。
                    output["answers"][0]["p_true"] = p_true
                    output["evidence_notes"][0]["known_evidence"] += f"合成序号{env}{point}{agent}。"
                    record = m.parse_subject_measurement_output(
                        dumps(output), q, visible_card_ids=["CARD_01"], timepoint=point)
                    entries.append({"parsed_measurement": record, "gold_bundle": deepcopy(private),
                                    "world_id": f"W_{env}", "agent_id": f"S{agent:02d}",
                                    "environment": env, "arm": None, "timepoint": point,
                                    "block_id": "B01"})
        registry = m.create_blind_sample_registry(entries, registry_seed=17)
        panel = m.reconcile_blind_evaluations(
            registry, registry_evaluations(registry), registry_measurements(registry), private)
        self.assertEqual(panel["contract"], "reconciled_blind_panel_v1")
        self.assertEqual(panel["expected_samples"], 64)
        aggregation = analysis.aggregate_panel_to_networks(
            panel, expected_agents_per_world=8, allow_demo=True)
        self.assertEqual(len(aggregation["network_records"]), 8)
        self.assertTrue(all(row["valid"] for row in aggregation["network_records"]))
        exposure = analysis.analyze_exposure_2x2(
            aggregation, target_topics=["SYNTHETIC"], allow_demo=True)
        self.assertTrue(exposure["overall"]["estimable"])
        self.assertEqual(set(exposure["overall"]["contrasts"]),
                         {"homogeneous", "misinformation", "interaction"})
        self.assertTrue(exposure["is_demo"])
        self.assertFalse(exposure["formal_eligible"])
        self.assertEqual(exposure["new_model_calls"], 0)
        self.assertEqual(panel["panel_sha256"], aggregation["source_panel_sha256"])

    def test_20_all_public_apis_never_mutate_inputs(self):
        aggregation = self.aggregate(exposure_panel(), expected_agents_per_world=8)
        aggregation_before = deepcopy(aggregation)
        analysis.analyze_exposure_2x2(aggregation, target_topics=["TOPIC_A"], allow_demo=True)
        self.assertEqual(aggregation, aggregation_before)
        intervention = self.aggregate(intervention_panel(), expected_agents_per_world=8)
        intervention_before = deepcopy(intervention)
        allocations = allocation_for()
        allocations_before = deepcopy(allocations)
        analysis.analyze_intervention_effects(
            intervention, target_topics=["TOPIC_A"], allocations=allocations,
            allow_demo=True, bootstrap_reps=20, permutation_reps=20)
        self.assertEqual(intervention, intervention_before)
        self.assertEqual(allocations, allocations_before)

    def test_21_public_api_exports_and_versions(self):
        self.assertEqual(set(analysis.__all__),
                         {"aggregate_panel_to_networks", "analyze_exposure_2x2",
                          "analyze_intervention_effects"})
        self.assertEqual(analysis.ANALYSIS_VERSION, "analysis_contract_v1")
        self.assertEqual(analysis.NETWORK_AGGREGATION_VERSION, "network_aggregation_v1")

    def test_22_json_safe_and_no_credential_like_text(self):
        aggregation = self.aggregate(intervention_panel(), expected_agents_per_world=8)
        result = analysis.analyze_intervention_effects(
            aggregation, target_topics=["TOPIC_A"], allocations=allocation_for(),
            allow_demo=True, bootstrap_reps=20, permutation_reps=20)
        text = dumps_json(result)
        runtime.canonical_hash(result)
        self.assertNotIn("sk-", text)
        self.assertIn("paired_network_block_not_individual_agent", result["independent_unit"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
