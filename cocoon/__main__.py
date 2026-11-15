"""命令行入口。只有mimo-smoke/mimo-assistant加--execute会请求收费模型。"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
import uuid

from .configuration import call_plan, load_config, readiness
from .engineering import dry_run
from .reporting import render_status, render_adapter_report
from .assistant_checks import inspect_adapter, run_adapter_checks
from .runtime import utc_now, verify_events, write_json_new
from .mimo_probe import mimo_status, run_probe
from .mimo import MiMoError
from .budget import BudgetError

ROOT = Path(__file__).resolve().parents[1]


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def newest_validation():
    files = sorted((ROOT / "experiment/review").glob("validation_*.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="破茧实验：默认离线。小米探测须显式--execute，使用已授权累计预算；不开放全量实验。")
    parser.add_argument("command", choices=["plan", "preflight", "dry-run", "verify", "validate", "report", "run", "record-protocol", "assistant-status", "assistant-check", "assistant-report", "mimo-status", "mimo-smoke", "mimo-assistant"])
    parser.add_argument("--config", type=Path, default=ROOT / "configs/core.json")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--execute", action="store_true", help="显式运行收费的MiMo小规模探测；总计受已批准的¥5预算限制")
    parser.add_argument("--packet", type=Path, help="明确指定待发送给小米的冻结证据包JSON")
    parser.add_argument("--user-view", type=Path, help="明确指定镜像阶段的个人观点纯文本文件")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "plan":
            emit(call_plan(config))
        elif args.command in ("preflight", "run"):
            result = readiness(config, ROOT)
            if args.command == "run":
                result["error"] = "当前版本没有正式模型执行入口；拒绝回退到演示数据。"
            emit(result)
            return 2
        elif args.command == "dry-run":
            summary = dry_run(config, ROOT)
            report = render_status(Path(summary["run_directory"]) / "status.html", readiness(config, ROOT), summary, newest_validation())
            emit({"status": "engineering_passed", "notice": summary["notice"], "run_directory": summary["run_directory"],
                  "report": str(report), "verification": summary["verification"],
                  "prepared_contexts": summary["prepared_contexts"], "snapshot_count": summary["snapshot_count"],
                  "actual_model_calls": 0, "formal_observations": 0})
        elif args.command == "verify":
            if not args.run_dir:
                parser.error("verify 需要 --run-dir")
            emit(verify_events(args.run_dir))
        elif args.command == "report":
            if not args.run_dir:
                parser.error("report 需要 --run-dir")
            summary = json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6]
            target = ROOT / "experiment/exports" / f"执行启动状态_{stamp}.html"
            render_status(target, readiness(config, ROOT), summary, newest_validation())
            emit({"report": str(target)})
        elif args.command == "mimo-status":
            state = mimo_status(config, ROOT)
            emit(state)
            return 2 if state["blockers"] else 0
        elif args.command in ("mimo-smoke", "mimo-assistant"):
            if args.command == "mimo-assistant" and not args.packet:
                parser.error("mimo-assistant 需要 --packet，绝不自动选用演示材料")
            if args.command == "mimo-smoke" and (args.packet or args.user_view):
                parser.error("mimo-smoke 只发送固定连通性问题，不接受材料")
            result = run_probe(config, ROOT, execute=args.execute, packet_path=args.packet, user_view_path=args.user_view)
            emit(result)
            return 0 if result.get("status") == "succeeded" else 2 if not result.get("executed") else 1
        elif args.command == "assistant-check":
            summary = run_adapter_checks(config, ROOT)
            emit({"status": summary["status"], "notice": summary["notice"],
                  "run_directory": summary["run_directory"], "scenarios": len(summary["scenarios"]),
                  "scenarios_passed": sum(case["passed"] for case in summary["scenarios"]),
                  "fixture_calls": summary["fixture_calls"], "actual_model_calls": 0,
                  "original_files_unchanged": summary["original_files_unchanged"]})
            return 0 if summary["status"] == "passed" else 1
        elif args.command in ("assistant-status", "assistant-report"):
            if args.run_dir:
                config["assistant"]["offline_check_directory"] = str(args.run_dir.resolve())
            status = inspect_adapter(config, ROOT)
            if args.command == "assistant-status":
                emit({key: value for key, value in status.items() if key != "summary"})
            else:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6]
                target = ROOT / "experiment/exports" / f"助手实验适配验收_{stamp}.html"
                render_adapter_report(target, status, newest_validation())
                emit({"report": str(target), "offline_verified": status["offline_verified"], "live_verified": False})
        elif args.command == "validate":
            stream = io.StringIO()
            suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6]
            target = ROOT / "experiment/review" / f"validation_{stamp}.json"
            data = {"created_at_utc": utc_now(), "tests_run": result.testsRun, "failures": len(result.failures),
                    "errors": len(result.errors), "skipped": len(result.skipped), "success": result.wasSuccessful(),
                    "scope": "离线工程单元测试；不包含真实API、OASIS或人工审核",
                    "test_log": stream.getvalue(), "evidence_policy": "测试目录保留，不自动删除"}
            write_json_new(target, data)
            emit({**data, "report": str(target)})
            return 0 if result.wasSuccessful() else 1
        elif args.command == "record-protocol":
            if not args.source or not args.source.is_file():
                parser.error("record-protocol 需要 --source 指定原方案文件")
            source = args.source.read_bytes()
            record = {"source_path": str(args.source.resolve()), "sha256": hashlib.sha256(source).hexdigest(),
                      "bytes": len(source), "recorded_at_utc": utc_now(), "source_unchanged": True,
                      "implementation_version": "0.1.0", "formal_frozen": False}
            target = ROOT / "experiment/manifests" / ("protocol_source_" + record["sha256"][:12] + ".json")
            if target.exists():
                emit(json.loads(target.read_text(encoding="utf-8")))
            else:
                write_json_new(target, record)
                emit(record)
    except (MiMoError, BudgetError) as exc:
        emit({"status": "failed", "error_code": exc.code, "demo_fallback": False})
        return 1
    except (ValueError, OSError, AssertionError) as exc:
        emit({"status": "failed", "error_type": type(exc).__name__, "message": str(exc), "demo_fallback": False})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
