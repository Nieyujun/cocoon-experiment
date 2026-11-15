"""导出离线结案交付归档包（白名单文件 + SHA-256 清单）。

用法（项目根目录）：
    python scripts/export_closeout_archive.py

只收录白名单文件；不收录原始网页全文、不收录任何密钥。归档时间戳固定，
便于重复构建与比对。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiment/exports/cocoon_closeout_dashboard_20261110.zip"
DASH = ROOT / "experiment/dashboard_20261110"
FIXED_DATE = (2026, 11, 10, 0, 0, 0)
CREDENTIAL = re.compile(
    r"sk-[A-Za-z0-9]{8,}|api[_-]?key\s*[:=]|Authorization\s*:|Bearer\s+[A-Za-z0-9._-]{10,}",
    re.I)


def _files():
    items = [
        (DASH / "dashboard.html", "dashboard.html"),
        (DASH / "dashboard_data.json", "dashboard_data.json"),
        (DASH / "verification.json", "verification.json"),
        (DASH / "REPORT_step37_20261110.md", "REPORT_step37_20261110.md"),
        (DASH / "REPORT_step38_20261110.md", "REPORT_step38_20261110.md"),
        (ROOT / "experiment/review/live_cross_topic_20261111/six_round_live_extractive_pairs.json",
         "live_cross_topic_20261111/six_round_live_extractive_pairs.json"),
        (ROOT / "experiment/review/live_micro_pilot_20261111/topic01_live_micro_pilot.json",
         "live_micro_pilot_20261111/topic01_live_micro_pilot.json"),
        (ROOT / "experiment/review/live_micro_pilot_20261111/topic02_live_micro_pilot.json",
         "live_micro_pilot_20261111/topic02_live_micro_pilot.json"),
        (ROOT / "experiment/review/live_micro_pilot_20261111/topic03_live_micro_pilot.json",
         "live_micro_pilot_20261111/topic03_live_micro_pilot.json"),
        (ROOT / "experiment/review/live_micro_pilot_20261111/three_topic_live_micro_pilot_matrix.json",
         "live_micro_pilot_20261111/three_topic_live_micro_pilot_matrix.json"),
        (ROOT / "experiment/review/live_micro_pilot_20261111/closed_world_synth01_live_pilot.json",
         "live_micro_pilot_20261111/closed_world_synth01_live_pilot.json"),
        (ROOT / "experiment/review/FINAL_PROJECT_REPORT_20261114.md", "FINAL_PROJECT_REPORT_20261114.md"),
        (ROOT / "experiment/review/DEFENSE_PRESENTATION_GUIDE_20261114.md",
         "DEFENSE_PRESENTATION_GUIDE_20261114.md"),
        (ROOT / "experiment/review/ai_assisted_review_20261110/scheme_a_merged_verified.json",
         "scheme_a_merged_verified.json"),
        (ROOT / "experiment/review/cross_topic_20261108/review.html",
         "cross_topic_20261108/review.html"),
        (ROOT / "PROGRESS.md", "PROGRESS.md"),
    ]
    reports = sorted((ROOT / "experiment/review").rglob("REPORT_step*.md"))
    for path in reports:
        relative = path.relative_to(ROOT).as_posix()
        items.append((path, relative))
    return items


def main():
    items = _files()
    missing = [str(path) for path, _ in items if not path.is_file()]
    if missing:
        print(json.dumps({"status": "failed", "missing": missing}, ensure_ascii=False))
        return 1
    manifest = []
    for path, arcname in items:
        data = path.read_bytes()
        if "intake/" in arcname or arcname.endswith(".source.json"):
            raise ValueError("归档不得包含原始网页或取证底稿：" + arcname)
        if CREDENTIAL.search(data.decode("utf-8", errors="ignore")):
            raise ValueError("归档条目疑似包含凭据：" + arcname)
        manifest.append({"path": arcname, "bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()})
    lines = ["# 离线结案交付包索引", "",
             f"共 {len(manifest)} 个文件；每项附 SHA-256。", "",
             "| 文件 | 字节 | SHA-256 |", "|---|---:|---|"]
    for row in manifest:
        lines.append(f"| `{row['path']}` | {row['bytes']} | `{row['sha256']}` |")
    lines += ["", "## 说明", "",
              "- 仅含白名单产物；不含原始网页全文、取证底稿或任何密钥。",
              "- `dashboard.html` 单文件自包含，本地打开即可；无外部 CDN 请求。",
              "- 看板为离线系统预演与方案A核验，不是真实模型认知实验结果。",
              "- 步骤38已修复时间轴胶囊行内块撕裂与筛选区布局缺陷，并为全部视图补齐"
              "表格横向滚动保护、网络网格与单一 sticky 顶栏。",
              "- 步骤39 六轮真实组织侧与原句摘录配对清单随包提供（含 Run ID、Token 与费用）。\n- `PROGRESS.md` 为项目进度索引；步骤28–42 的 Markdown 汇报原样收录。\n- 步骤41 三议题真实端到端实证清单（TOPIC_01/02/03）与三议题汇总矩阵随包提供（每议题 n=2×3，仅描述性对比）。\n- 步骤40 TOPIC_01 真实微型实证清单随包提供（n=2×3 机制演示，非统计推断；含被取代运行与中断持有预留披露）。\n- 步骤42 §5.4 封闭虚构情境 TOPIC_SYNTH_01（星澜市）真实实证清单随包提供：全部条目标 synthetic，不作为现实世界效果证据；另附总结案报告与五人答辩攻略。"]
    manifest_md = "\n".join(lines) + "\n"
    manifest_json = json.dumps({"contract": "closeout_archive_v1",
                                "files": manifest,
                                "total_bytes": sum(row["bytes"] for row in manifest),
                                "contains_raw_web_pages": False,
                                "contains_credentials": False},
                               ensure_ascii=False, indent=2)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, arcname in items:
            info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
        for arcname, text in (("MANIFEST.md", manifest_md), ("MANIFEST.json", manifest_json)):
            info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text.encode("utf-8"))
    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
    sidecar = OUT.with_suffix(OUT.suffix + ".sha256")
    sidecar.write_text(digest + "  " + OUT.name + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": "ok", "path": str(OUT), "bytes": OUT.stat().st_size,
                      "entries": len(items) + 2, "zip_sha256": digest,
                      "sidecar": str(sidecar),
                      "reports": sum(1 for row in manifest
                                     if row["path"].endswith(".md") and "REPORT_step" in row["path"])},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
