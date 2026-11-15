"""5 分钟答辩演示页 v3（高定科学编辑部×精密光学沙盘 showcase_5min.html）离线专项测试。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest import mock

from cocoon import showcase as sc
from cocoon.runtime import canonical_hash

ROOT = Path(__file__).resolve().parents[1]
DASHDIR = ROOT / "experiment/dashboard_20261110"
DATASET_PATH = DASHDIR / "dashboard_data.json"
CLOSED_MANIFEST = ROOT / "experiment/review/live_micro_pilot_20261111/closed_world_synth01_live_pilot.json"
LEDGER = ROOT / "experiment/billing/mimo_initial_probe_v1.jsonl"

PROTECTED = [
    DASHDIR / "dashboard.html",
    DASHDIR / "dashboard_data.json",
    DASHDIR / "verification.json",
    CLOSED_MANIFEST,
    ROOT / "experiment/review/live_micro_pilot_20261111/three_topic_live_micro_pilot_matrix.json",
    ROOT / "experiment/review/FIVE_MINUTE_SHOWCASE_SCRIPT_20261114.md",
    LEDGER,
    ROOT / "cocoon/dashboard.py",
    ROOT / "cocoon/closed_world_pilot.py",
    ROOT / "configs/core.json",
    ROOT / "破茧助手全量实验方案_v1.md",
    ROOT / "PROGRESS.md",
]
PROTECTED_PREFIXES = {
    DASHDIR / "dashboard.html": "46c6a0dc",
    DASHDIR / "dashboard_data.json": "6437e3a5",
    DASHDIR / "verification.json": "5397d08e",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ShowcaseV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected_before = {p: _sha(p) for p in PROTECTED}
        cls.tmp = Path(tempfile.mkdtemp(prefix="showcase_v3_test_"))
        cls.out_html = cls.tmp / "showcase_5min.html"
        with mock.patch("socket.socket", side_effect=AssertionError("network_forbidden")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network_forbidden")):
            cls.payload = sc.build_showcase_payload(DATASET_PATH)
            cls.render_info = sc.render_five_minute_showcase(cls.out_html, DATASET_PATH)
            cls.verify_info = sc.verify_five_minute_showcase(cls.out_html, DATASET_PATH)
        cls.html_text = cls.out_html.read_text(encoding="utf-8")
        cls.raw_dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        cls.manifest = json.loads(CLOSED_MANIFEST.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        after = {p: _sha(p) for p in PROTECTED}
        if after != cls.protected_before:
            raise AssertionError("受保护原件哈希在演示页测试期间发生改变")

    def _expect_reject(self, old, new, code, *, count=1):
        self.assertIn(old, self.html_text)
        path = self.tmp / f"bad_{code}.html"
        path.write_text(self.html_text.replace(old, new, count), encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, code):
            sc.verify_five_minute_showcase(path, DATASET_PATH)

    # ---------------------------------------------------------------- 数据与确定性
    def test_01_contract_v3_deterministic_and_byte_reproducible(self):
        self.assertTrue(self.verify_info["passed"])
        self.assertEqual(sc.SHOWCASE_CONTRACT, "five_minute_showcase_v3")
        self.assertEqual(self.payload["contract"], "five_minute_showcase_v3")
        again = sc.build_showcase_payload(DATASET_PATH)
        self.assertEqual(again, self.payload)
        self.assertEqual(
            self.payload["showcase_sha256"],
            canonical_hash({k: v for k, v in self.payload.items() if k != "showcase_sha256"}),
        )
        self.assertEqual(self.payload["source_dataset_sha256"], self.raw_dataset["dataset_sha256"])
        second = self.tmp / "repeat.html"
        sc.render_five_minute_showcase(second, DATASET_PATH)
        self.assertEqual(second.read_bytes(), self.out_html.read_bytes())
        self.assertEqual(len(self.payload["scenes"]), 4)
        self.assertEqual([s["subject_id"] for s in self.payload["subjects"]], ["S01", "S02"])

    def test_02_verbatim_model_texts_match_sources(self):
        closed = {s["subject"]: s for s in self.raw_dataset["closed_world_pilot"]["subjects"]}
        for subj in self.payload["subjects"]:
            sid, src = subj["subject_id"], closed[subj["subject_id"]]
            self.assertEqual(subj["speech"], src["speech"])
            self.assertEqual([m["text"] for m in subj["mirror"]["items"]], [m["text"] for m in src["mirror_items"]])
            self.assertEqual(subj["exposure"]["total"], 48)
            self.assertEqual(subj["exposure"]["same"], 32)
            self.assertEqual(subj["exposure"]["rumor"], 16)
            self.assertEqual(len(subj["feed"]), 48)
            self.assertEqual(subj["parent_snapshot_sha256"], self.manifest["parent_t1_core_sha256"][sid])
            self.assertEqual(subj["t1_items_at_half"], 12)
            for r_key, rc in subj["rounds"].items():
                for arm in ("I4", "I5"):
                    visible = self.manifest["interventions"][sid][arm][r_key]["visible_text"]
                    self.assertEqual(rc[arm.lower() + "_chars"], len(visible))
                    self.assertTrue(600 <= rc[arm.lower() + "_chars"] <= 800)
                    self.assertEqual(rc[arm.lower() + "_addition"], src[arm.lower() + "_additions"][r_key])
                    self.assertIn(rc["shared_evidence_block"], visible)
                self.assertEqual(len(rc["pair_checks"]), 7)
                self.assertTrue(all(rc["pair_checks"].values()))
                for arm in sc.ARMS:
                    self.assertEqual(rc["responses"][arm], src["intervention_responses"][arm][int(r_key) - 1])
        evidence_texts = {e["evidence_id"]: e["text"] for e in self.payload["evidence"]}
        self.assertEqual(len(evidence_texts), 6)
        for subj in self.payload["subjects"]:
            for card in subj["feed"]:
                self.assertEqual(card["misleading"], bool(card["evidence_ids"]))
                for eid in card["evidence_ids"]:
                    self.assertIn(eid, evidence_texts)

    def test_03_stage_quotes_are_verbatim_substrings(self):
        for subj in self.payload["subjects"]:
            self.assertEqual(len(subj["bubbles"]), 3)
            for text in subj["speech_highlights"]:
                self.assertIn(text, subj["speech"])
            for rc in subj["rounds"].values():
                for arm in sc.ARMS:
                    bubble = rc["bubbles"][arm]
                    self.assertLessEqual(len(bubble), 24)
                    self.assertIn(bubble, rc["responses"][arm])
                    self.assertIn(sc._e("「" + bubble + "」"), self.html_text)
            for target in subj["mirror"]["targets"]:
                self.assertTrue(
                    subj["mirror"]["items"][target["index"]]["text"].startswith(target["label"])
                    or target["kind"] == "blindspot"
                )

    def test_04_dual_track_and_final_scoreboard_are_honest(self):
        self.assertEqual(self.payload["real_track"]["acc_series"]["I4"][0], 100.0)
        self.assertEqual(self.payload["synth_track"]["acc_series"]["I0"], [50.0, 50.0, 50.0, 50.0])
        self.assertEqual(self.payload["synth_track"]["acc_series"]["I5"], [50.0, 50.0, 100.0, 100.0])
        self.assertEqual(self.payload["synth_track"]["acc_series"]["I4"], [50.0, 50.0, 100.0, 100.0])
        sb = {r["arm"]: r for r in self.payload["scoreboard"]}
        self.assertEqual(sb["I4"]["bs_t3"], 0.0025)
        self.assertEqual(sb["I5"]["bs_t3"], 0.0025)
        self.assertEqual((sb["I4"]["transfer_correct_t3"], sb["I5"]["transfer_correct_t3"]), (4.0, 3.0))
        self.assertEqual((sb["I4"]["u_t3"], sb["I5"]["u_t3"]), (100.0, 91.7))
        self.assertEqual((sb["I4"]["o_t3"], sb["I5"]["o_t3"]), (58.3, 33.3))
        rows = {r["key"]: r for r in self.payload["final_rows"]}
        self.assertTrue(rows["o"]["lower_better"])
        self.assertIn("I5 33.3 优于 I4 58.3", rows["o"]["note"])
        self.assertIn("打平", rows["acc"]["note"])
        self.assertIn("越低越好：I5 33.3 优于 I4 58.3", self.html_text)
        self.assertEqual(self.payload["clone"]["max_difference_pct"], 2.6)
        self.assertEqual(self.payload["scale"]["final_run_calls"], 217)
        self.assertEqual(self.payload["scale"]["answers_and_blind_evaluations"], 128)

    def test_05_honest_boundary_and_offline_self_contained(self):
        badge = self.payload["honest_badge"]
        for token in ("mimo-v2.6-flash", "n=2", "§5.4", "formal_eligible=False"):
            self.assertIn(token, badge)
        self.assertIn('id="honest-corner"', self.html_text)
        q5 = self.payload["qa_cards"][4]["a"]
        self.assertIn("XL_E03", q5)
        self.assertIn("XL_E06", q5)
        flags = [rc["review_flag"] for s in self.payload["subjects"] for rc in s["rounds"].values() if rc["review_flag"]]
        self.assertEqual(len(flags), 1)
        self.assertIn("待真人复核：证据编号错位", self.html_text)
        self.assertIn("1 处引用错位待复核", self.html_text)
        self.assertTrue(any("真人受试 0" in x for x in self.payload["boundary"]))
        self.assertIn('href="./dashboard.html"', self.html_text)
        stripped = sc._strip_data(self.html_text)
        self.assertNotIn("http://", stripped)
        self.assertNotIn("https://", stripped)
        self.assertNotIn("fetch(", stripped)

    # ---------------------------------------------------------------- 反 AI 味与反简笔画门禁拦截
    def test_06_emoji_injection_rejected(self):
        self._expect_reject('id="btn-timer">计时</button>', 'id="btn-timer">\U0001F3AC 计时</button>', "ui_emoji_forbidden")
        self._expect_reject("<h2>", "<h2>\u2728", "ui_emoji_forbidden")

    def test_07_gradient_text_and_button_injection_rejected(self):
        self._expect_reject(
            ".scene-head h2{\n  margin:0;",
            ".scene-head h2{\n  -webkit-background-clip:text;background-clip:text;margin:0;",
            "ui_gradient_text_forbidden",
        )
        self._expect_reject(
            ".btn{\n  font-size:12.5px;",
            ".btn{\n  background-image:linear-gradient(90deg,#60f,#f90);font-size:12.5px;",
            "ui_gradient_button_forbidden",
        )

    def test_08_harsh_block_shadow_injection_rejected(self):
        self._expect_reject(
            ".btn.on{background:var(--ink);",
            ".btn.on{box-shadow:3px 3px 0 #111;background:var(--ink);",
            "ui_harsh_shadow_forbidden",
        )

    def test_09_legacy_stick_figure_injection_rejected(self):
        self._expect_reject(
            '<circle class="ag-rim" cx="0" cy="0" r="22"/>',
            '<circle class="ag-rim" cx="0" cy="0" r="22"/><path d="M14 -42 L72 -64 L72 -18 Z"/>',
            "ui_stick_figure_forbidden",
        )

    def test_10_missing_optical_svg_defs_rejected(self):
        self._expect_reject("<radialGradient", "<g data-removed-rad", "ui_optical_defs_missing", count=-1)

    def test_11_teleprompter_and_drawer_must_start_closed(self):
        self._expect_reject(
            '<aside id="teleprompter" class="tele hidden"',
            '<aside id="teleprompter" class="tele"',
            "ui_teleprompter_not_hidden",
        )
        self._expect_reject(
            '<aside id="drawer" class="drawer" aria-hidden="true"',
            '<aside id="drawer" class="drawer open" aria-hidden="false"',
            "ui_drawer_not_closed",
        )
        self._expect_reject('<div id="scrim" class="scrim" hidden>', '<div id="scrim" class="scrim">', "ui_scrim_not_hidden")

    def test_12_scene_text_wall_rejected(self):
        lede = self.payload["scenes"][1]["lede"]
        self._expect_reject(
            f'<p class="lede">{lede}',
            f'<p class="lede">{lede}这里额外塞进一大段说明文字，模拟旧版主屏堆砌上千字文本墙的写法。',
            "ui_scene_1_text_over_limit",
        )

    def test_13_missing_sandbox_stage_rejected(self):
        self._expect_reject('<svg class="sandbox" id="track-stage"', '<svg class="sandboxx" id="track-stage"',
                            "ui_scene_2_sandbox_missing")

    # ---------------------------------------------------------------- 篡改与安全
    def test_14_embedded_data_tamper_rejected(self):
        self._expect_reject('"o_t3":58.3', '"o_t3":18.3', "embedded_payload_mismatch")

    def test_15_changes_outside_embedded_data_rejected(self):
        self._expect_reject("setInterval(tickTimer,1000)", "setInterval(tickTimer,100)", "html_document_mismatch")
        self._expect_reject("主终点 Brier 同为 0.0025 · 打平", "I4 效果显著领先", "html_document_mismatch")

    def test_16_bad_dataset_external_and_credentials_rejected(self):
        dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
        dataset["overview"]["panel_samples"] += 1
        bad = self.tmp / "bad_dataset.json"
        bad.write_text(json.dumps(dataset), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "dataset_hash_mismatch"):
            sc.build_showcase_payload(bad)
        self._expect_reject("<body ", '<body ><img src="https://evil.example/p.png"', "external_resource_forbidden")
        self._expect_reject("<body ", "<!-- sk-FAKE123456789012 --><body ", "credential_leak_detected")

    def test_17_renderer_cannot_overwrite_protected_files(self):
        for path in (DATASET_PATH, DASHDIR / "dashboard.html", DASHDIR / "verification.json", ROOT / "PROGRESS.md"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "output_is_source_dataset|output_would_modify"):
                    sc.render_five_minute_showcase(path, DATASET_PATH)

    def test_18_builder_requires_checks_parent_and_verbatim_quotes(self):
        from cocoon import closed_world_pilot
        closed = self.raw_dataset["closed_world_pilot"]
        materials = closed_world_pilot.build_closed_world_materials()
        bad_closed = deepcopy(closed)
        bad_closed["subjects"][0]["pair_checks"]["1"].pop("evidence_equal")
        with self.assertRaisesRegex(ValueError, "pair_checks_failed"):
            sc._build_subjects_payload(bad_closed, materials, self.manifest)
        bad_manifest = deepcopy(self.manifest)
        bad_manifest["branches"][0]["pre_treatment_core_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "parent_snapshot_mismatch"):
            sc._build_subjects_payload(closed, materials, bad_manifest)
        rewritten = dict(sc._TRACK_BUBBLES)
        rewritten[("S01", "1")] = dict(rewritten[("S01", "1")], I4="受试表示破茧助手让他豁然开朗")
        with mock.patch.object(sc, "_TRACK_BUBBLES", rewritten):
            with self.assertRaisesRegex(ValueError, "track_bubble_not_verbatim"):
                sc._build_subjects_payload(closed, materials, self.manifest)
        with mock.patch.dict(sc._SPEECH_HIGHLIGHTS, {"S02": ["准点率98%"]}):
            with self.assertRaisesRegex(ValueError, "speech_highlight_not_verbatim"):
                sc._build_subjects_payload(closed, materials, self.manifest)

    # ---------------------------------------------------------------- 生产文件与受保护原件
    def test_19_production_showcase_file_verified(self):
        prod = DASHDIR / "showcase_5min.html"
        self.assertTrue(prod.is_file())
        result = sc.verify_five_minute_showcase(prod, DATASET_PATH)
        self.assertTrue(result["passed"])
        self.assertEqual(result["contract"], "five_minute_showcase_v3")
        self.assertEqual(prod.read_bytes(), self.out_html.read_bytes())

    def test_20_protected_originals_unchanged(self):
        for path, prefix in PROTECTED_PREFIXES.items():
            self.assertTrue(_sha(path).startswith(prefix), path)
        with LEDGER.open("rb") as fh:
            self.assertEqual(sum(1 for _ in fh), 772)
        self.assertEqual({p: _sha(p) for p in PROTECTED}, self.protected_before)

    def test_21_payload_pinned_to_archived_validation(self):
        self.assertEqual(sc.VALIDATION_PATH.name, "validation_20261114T063037_ac1280.json")
        self.assertEqual(self.payload["scale"]["archived_tests"], 1031)
        with mock.patch.object(Path, "glob", side_effect=AssertionError("payload_must_not_scan_directories")), \
             mock.patch.object(Path, "iterdir", side_effect=AssertionError("payload_must_not_scan_directories")):
            again = sc.build_showcase_payload(DATASET_PATH)
        self.assertEqual(again["showcase_sha256"], self.payload["showcase_sha256"])

    def test_22_sandbox_v3_structure_and_controls(self):
        audit = self.verify_info["ui_sandbox_audit"]
        self.assertEqual(audit["emoji_hits"], 0)
        self.assertEqual(audit["harsh_block_shadow"], 0)
        self.assertEqual(audit["stick_figure"], 0)
        self.assertTrue(audit["optical_defs"])
        self.assertEqual(audit["sandbox_stages"], ["clone-stage", "dye-stage", "track-stage", "final-stage"])
        self.assertTrue(all(n <= 45 for n in audit["scene_text_chars"]))
        for scene in self.payload["scenes"]:
            self.assertLessEqual(len(sc.CJK.findall(scene["lede"])), 40)
        markup = sc._strip_data(self.html_text)
        self.assertEqual(len(sc.EMOJI.findall(markup)), 0)
        for sid in ("S01", "S02"):
            self.assertIn(f'id="dye-{sid}" data-only-subject="{sid}"', markup)
            for r in ("1", "2"):
                self.assertIn(f'data-only-subject="{sid}" data-only-round="{r}"', markup)
        self.assertEqual(markup.count('class="beacon r'), 96)
        self.assertEqual(markup.count('class="beacon r') - markup.count(' rumor"'), 64)
        self.assertIn('class="lens-unit"', markup)
        self.assertIn('100% 同一证据块', markup)
        for marker in ("ArrowRight", "ArrowLeft", "Escape", "toggleTimer", "toggleTheme",
                       "tele.classList.toggle('hidden')", "beginElement", "data-metric-btn"):
            self.assertIn(marker, markup)
        self.assertEqual(self.verify_info["truth_audit"]["rounds_checked"], 4)
        self.assertEqual(
            re.findall(r'<section class="scene[^"]*" id="(scene-\d)"', markup),
            ["scene-0", "scene-1", "scene-2", "scene-3"],
        )


if __name__ == "__main__":
    unittest.main()
