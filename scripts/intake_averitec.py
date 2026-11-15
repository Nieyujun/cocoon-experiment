"""仅接入 AVeriTeC 原版 dev.json 的索引 0；所有产物均为未批准的私有候选。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
INTAKE = ROOT / "experiment" / "intake"
STATUS_PATH = ROOT / "experiment" / "review" / "intake_status.json"
REPOSITORY = "https://github.com/MichSchli/AVeriTeC"
BRANCH = "main"
COMMIT = "7c62d1ec8df3fb560d6efe2b85fa191135636f81"
SOURCE_URL = f"https://raw.githubusercontent.com/MichSchli/AVeriTeC/{COMMIT}/data/dev.json"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc/4.0/"
MAX_BYTES = 64 * 1024 * 1024
PRODUCER = "intake_averitec.py"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def write_new_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "拒绝重定向，不切换地址重试", headers, None)


def count_and_select(raw, index):
    """仅扫描 JSON 容器边界计数；不解码其他记录的文本或进行主题筛选。"""
    if index != 0:
        raise ValueError("仅允许指定索引 0")
    start = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    size = len(raw)

    def skip_space(pos):
        while pos < size and raw[pos] in b" \t\r\n":
            pos += 1
        return pos

    pos = skip_space(start)
    if pos >= size or raw[pos] != ord("["):
        raise ValueError("响应不是 JSON 数组")
    pos = skip_space(pos + 1)
    count = 0
    selected = None
    while pos < size and raw[pos] != ord("]"):
        if raw[pos] != ord("{"):
            raise ValueError("记录不是 JSON 对象")
        begin = pos
        stack = []
        in_string = escaped = False
        while pos < size:
            char = raw[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == ord("\\"):
                    escaped = True
                elif char == ord('"'):
                    in_string = False
            elif char == ord('"'):
                in_string = True
            elif char in (ord("{"), ord("[")):
                stack.append(char)
            elif char in (ord("}"), ord("]")):
                opening = ord("{") if char == ord("}") else ord("[")
                if not stack or stack.pop() != opening:
                    raise ValueError("JSON 容器边界不匹配")
            pos += 1
            if not stack and not in_string:
                break
        if stack or in_string:
            raise ValueError("JSON 响应未完整结束")
        if count == index:
            selected = (begin, pos)
        count += 1
        pos = skip_space(pos)
        if pos < size and raw[pos] == ord(","):
            pos = skip_space(pos + 1)
            if pos >= size or raw[pos] == ord("]"):
                raise ValueError("JSON 数组存在空记录")
        elif pos >= size or raw[pos] != ord("]"):
            raise ValueError("JSON 数组分隔符异常")
    if pos >= size or skip_space(pos + 1) != size or selected is None:
        raise ValueError("JSON 数组不完整或索引不存在")
    # 只有指定记录进入 JSON 解码器，其余记录仅作字节边界计数。
    return count, json.loads(raw[selected[0]:selected[1]])


def topic_checks(claim):
    if not isinstance(claim, str) or not 1 <= len(claim) <= 1000:
        return {"claim_shape": False}
    return {
        "actor": bool(re.search(r"\b(actor|actress)\b", claim, re.I)),
        "letter": bool(re.search(r"\bletter\b", claim, re.I)),
        "refusal": bool(re.search(r"\b(refus\w*|declin\w*|reject\w*|turn\w*\s+down)\b", claim, re.I)),
        "advertisement": bool(re.search(r"\b(advertis\w*|adverts?|ads?|commercials?|endorse\w*)\b", claim, re.I)),
    }


def make_candidate(record, manifest):
    # 白名单构造，不复制原 label、justification、questions 或其他注释字段。
    return {
        "index": 0,
        "claim": record["claim"],
        "source": {
            "dataset": "AVeriTeC",
            "split": "dev",
            "url": manifest["url"],
            "commit": COMMIT,
            "sha256": manifest["sha256"],
            "retrieved_at_utc": manifest["retrieved_at_utc"],
            "license_url": LICENSE_URL,
            "license_declared": "CC BY-NC 4.0",
            "redistributable": False,
        },
        "review_status": "pending_dual_human_review",
        "approved_for_experiment": False,
    }


def check_output_parents():
    for directory in (INTAKE, STATUS_PATH.parent):
        if not directory.is_dir() or directory.is_symlink() or directory.is_junction():
            raise OSError("允许输出的父目录缺失或为路径重定向")
    if STATUS_PATH.exists():
        if STATUS_PATH.is_symlink():
            raise OSError("拒绝通过符号链接写入状态文件")
        previous = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        if previous.get("producer") != PRODUCER:
            raise FileExistsError("已有状态文件不是本脚本生成，拒绝覆盖")


def acquire():
    check_output_parents()
    version_root = INTAKE / "private" / "averitec" / COMMIT
    for directory in (INTAKE / "private", INTAKE / "private" / "averitec", version_root):
        if directory.exists() and (directory.is_symlink() or directory.is_junction()):
            raise OSError("拒绝在路径重定向目录中下载")
        directory.mkdir(mode=0o700, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid4().hex[:12]
    run_dir = version_root / run_id
    run_dir.mkdir(mode=0o700, exist_ok=False)
    raw_path = run_dir / "dev.response.partial"
    manifest_path = run_dir / "acquisition.json"
    candidate_path = run_dir / "candidate_index.json"
    started = utc_now()
    manifest = {
        "producer": PRODUCER,
        "schema_version": 1,
        "run_id": run_id,
        "dataset": "AVeriTeC",
        "split": "dev",
        "repository": REPOSITORY,
        "branch": BRANCH,
        "commit": COMMIT,
        "version_freeze_pending": False,
        "commit_verification": {
            "method": "GitHub 官方 API 经 WebFetch 核对；不是本机数据下载记录",
            "url": f"https://api.github.com/repos/MichSchli/AVeriTeC/branches/{BRANCH}",
            "readme_url": f"https://raw.githubusercontent.com/MichSchli/AVeriTeC/{COMMIT}/README.md",
        },
        "url": SOURCE_URL,
        "requested_at_utc": started,
        "retrieved_at_utc": None,
        "completed_at_utc": None,
        "http_status": None,
        "sha256": None,
        "bytes": 0,
        "row_count": None,
        "row_count_method": "扫描顶层对象边界；不解码其他记录；不验证其他记录的字段语义",
        "license_url": LICENSE_URL,
        "license_declared": "CC BY-NC 4.0",
        "license_declaration_source": "https://fever.ai/dataset/averitec.html",
        "redistributable": False,
        "rights_status": "pending_underlying_rights_and_human_review",
        "upstream_declaration_not_local_download": {"total_records": 4568, "label_class_count": 4},
        "selected_indices": [0],
        "other_records_decoded": 0,
        "political_material_screening_performed": False,
        "review_status": "pending_dual_human_review",
        "approved_for_experiment": False,
        "human_reviews_completed": 0,
        "evidence_pages_fetched": 0,
        "paid_model_calls": 0,
        "formal_material_count": 0,
        "candidate_count": 0,
        "raw_path": None,
        "candidate_path": None,
        "raw_download_complete": False,
        "access_control": {
            "storage_class": "private_intake",
            "subject_access_policy": "deny",
            "assistant_access_policy": "deny",
            "export_to_experiment": False,
            "runtime_access_boundary_verified": False,
            "note": "私有路径及用途约束不等于 Windows ACL 或运行时沙箱已验证；禁止接入实验直到边界验证通过",
        },
        "missing_conditions": [
            "两名人工独立复核及裁决记录",
            "底层主张、证据和网页权利与用途核验",
            "subject/assistant 对 private intake 的运行时访问隔离验证",
            "实验材料正式审核批准；当前未生成正式材料",
        ],
        "outcome": "acquisition_failed",
        "error": None,
    }
    digest = hashlib.sha256()
    try:
        request = Request(SOURCE_URL, headers={
            "User-Agent": "AVeriTeC-Private-Intake/1.0",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        })
        # 使用系统默认代理和 TLS 校验；无凭据、无重试、不接受重定向。
        with build_opener(NoRedirects()).open(request, timeout=45) as response:
            manifest["http_status"] = response.status
            if response.status != 200:
                raise ValueError("未收到完整的 HTTP 200 响应")
            manifest["retrieved_at_utc"] = utc_now()
            manifest["response_content_type"] = response.headers.get("Content-Type")
            manifest["response_content_length"] = response.headers.get("Content-Length")
            manifest["response_etag"] = response.headers.get("ETag")
            with raw_path.open("xb") as output:
                manifest["raw_path"] = raw_path.as_posix()
                while True:
                    block = response.read(65536)
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                    manifest["bytes"] += len(block)
                    if manifest["bytes"] > MAX_BYTES:
                        raise ValueError("响应超过允许大小，保留已获取字节并停止")
        manifest["sha256"] = digest.hexdigest()
        length = manifest["response_content_length"]
        if length is not None and int(length) != manifest["bytes"]:
            raise ValueError("实际字节数与响应声明长度不一致")
        # 目标位于新建的唯一版本目录；额外拒绝覆盖任何同名下载。
        complete_path = run_dir / "dev.json"
        if complete_path.exists():
            raise FileExistsError("拒绝覆盖已有下载")
        raw_path.rename(complete_path)
        raw_path = complete_path
        manifest["raw_path"] = raw_path.as_posix()
        manifest["raw_download_complete"] = True
        count, record = count_and_select(raw_path.read_bytes(), 0)
        manifest["row_count"] = count
        checks = topic_checks(record.get("claim"))
        manifest["single_index_topic_check"] = {
            "index": 0,
            "expected_topic": "演员拒绝广告的信件",
            "method": "只检查指定索引主张的四组主题词；不是事实裁定或人工审核",
            "checks": checks,
        }
        if all(checks.values()):
            write_new_json(candidate_path, make_candidate(record, manifest))
            manifest["candidate_count"] = 1
            manifest["candidate_path"] = candidate_path.as_posix()
            manifest["outcome"] = "candidate_pending_dual_human_review"
        else:
            manifest["outcome"] = "quarantined_topic_unconfirmed"
            manifest["missing_conditions"].append("索引 0 未能确认预期演员广告信件主题，不推荐且不检查其他索引")
    except (OSError, ValueError, URLError) as exc:
        if isinstance(exc, HTTPError):
            manifest["http_status"] = exc.code
        # 不记录响应正文、代理详情或原始数据片段。
        manifest["error"] = {"type": type(exc).__name__, "http_status": manifest["http_status"]}
        manifest["outcome"] = "quarantined_validation_failed" if manifest["raw_download_complete"] else "acquisition_failed"
        manifest["missing_conditions"].append("获取或校验未完成；查看错误类型，禁止切换通道或绕过权限拒绝")
    finally:
        if manifest["raw_path"] is not None:
            manifest["sha256"] = digest.hexdigest()
        manifest["completed_at_utc"] = utc_now()
        write_new_json(manifest_path, manifest)
        status = {
            key: manifest[key] for key in (
                "producer", "schema_version", "run_id", "outcome", "url", "commit",
                "version_freeze_pending", "requested_at_utc", "retrieved_at_utc", "completed_at_utc",
                "sha256", "bytes", "row_count", "license_url", "license_declared", "redistributable",
                "rights_status", "raw_path", "candidate_path", "raw_download_complete", "candidate_count",
                "review_status", "approved_for_experiment", "human_reviews_completed",
                "evidence_pages_fetched", "paid_model_calls", "formal_material_count", "access_control",
                "missing_conditions", "error",
            )
        }
        status["acquisition_manifest_path"] = manifest_path.as_posix()
        status["experiment_eligible_count"] = 0
        status["status_scope"] = "仅公开材料接入；不是实验启动或预算配置"
        # 每次原始下载和来源清单不可覆盖；状态文件仅作为最近一次运行的指针。
        check_output_parents()
        status_tmp = run_dir / "status.next.json"
        write_new_json(status_tmp, status)
        os.replace(status_tmp, STATUS_PATH)
    return status


def self_test():
    import unittest
    from unittest.mock import patch
    from tempfile import TemporaryDirectory

    class IntakeTests(unittest.TestCase):
        def test_only_index_zero_decoded(self):
            raw = json.dumps([
                {"claim": 'Actor wrote a letter declining an advertisement. [\\\"]'},
                {"claim": "普通占位文本", "items": [{"a": "},["}]},
            ]).encode()
            decoder = json.loads
            with patch.object(json, "loads", wraps=decoder) as load:
                count, record = count_and_select(raw, 0)
                self.assertEqual(count, 2)
                self.assertIn("Actor", record["claim"])
                self.assertEqual(load.call_count, 1)
                self.assertNotIn("普通占位文本".encode(), load.call_args.args[0])

        def test_rejects_other_indices_and_bad_envelopes(self):
            for raw, index in ((b"[]", 0), (b"[{},]", 0), (b"[{}", 0), (b"[{}]x", 0), (b"[{}]", 1)):
                with self.subTest(raw=raw, index=index), self.assertRaises(ValueError):
                    count_and_select(raw, index)

        def test_topic_requires_all_components(self):
            self.assertTrue(all(topic_checks("An actor wrote a letter refusing an advertisement.").values()))
            self.assertFalse(all(topic_checks("An actor wrote a letter about a film.").values()))
            self.assertFalse(all(topic_checks(None).values()))

        def test_candidate_field_allowlist(self):
            record = {"claim": "测试主张", "label": "私有占位", "questions": ["私有占位"], "justification": "私有占位"}
            candidate = make_candidate(record, {"url": SOURCE_URL, "sha256": "0" * 64, "retrieved_at_utc": utc_now()})
            self.assertEqual(set(candidate), {"index", "claim", "source", "review_status", "approved_for_experiment"})
            self.assertNotIn("私有占位", json.dumps(candidate, ensure_ascii=False))
            self.assertFalse(candidate["approved_for_experiment"])

        def test_existing_file_not_overwritten(self):
            with TemporaryDirectory(prefix="intake_test_", dir=INTAKE) as temporary:
                path = Path(temporary) / "test.json"
                write_new_json(path, {"value": 1})
                before = path.read_bytes()
                with self.assertRaises(FileExistsError):
                    write_new_json(path, {"value": 2})
                self.assertEqual(path.read_bytes(), before)

        def test_redirect_refused(self):
            with self.assertRaises(HTTPError):
                NoRedirects().redirect_request(Request(SOURCE_URL), None, 302, "", {}, REPOSITORY)

    return unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(IntakeTests)).wasSuccessful()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, choices=[0], help="必须显式指定唯一允许的索引 0")
    parser.add_argument("--self-test", action="store_true", help="仅运行离线自测，不发起网络请求")
    args = parser.parse_args()
    if args.self_test:
        return 0 if self_test() else 1
    if args.index is None:
        parser.error("接入必须显式传入 --index 0")
    try:
        status = acquire()
    except (OSError, ValueError) as exc:
        print(json.dumps({"outcome": "local_setup_or_status_write_failed", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(status, ensure_ascii=True, indent=2))
    return 0 if status["raw_download_complete"] and status["error"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
