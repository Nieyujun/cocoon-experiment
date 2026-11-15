"""公开HTML来源的单次隔离取证；不执行网页、不使用用户凭据、不自动重试。"""
import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_new(path, data):
    with path.open("x", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, allow_nan=False)


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocked = 0
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.blocked += 1
        elif tag in ("p", "h1", "h2", "h3", "li", "div", "section", "br", "tr"):
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self.blocked = max(0, self.blocked - 1)
        elif tag in ("p", "h1", "h2", "h3", "li", "div", "section", "tr"):
            self.parts.append("\n")
    def handle_data(self, text):
        if not self.blocked:
            self.parts.append(text)


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def capture(url, out, source_id):
    out = Path(out)
    if not out.is_dir():
        raise FileNotFoundError(out)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,30}", source_id):
        raise ValueError("source_id格式不符")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("只取HTTPS公开来源")
    paths = {"raw": out / (source_id + ".html"), "text": out / (source_id + ".text.json"),
             "meta": out / (source_id + ".source.json")}
    if any(path.exists() for path in paths.values()):
        raise FileExistsError("保留既有取证文件，禁止覆盖")
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl unavailable")
    started = utc_now()
    run = subprocess.run([curl, "--silent", "--show-error", "--location", "--max-redirs", "3",
          "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "15", "--max-time", "45",
          "--max-filesize", "8000000", "--output", str(paths["raw"]),
          "--write-out", "%{http_code}\n%{url_effective}\n%{content_type}", url], capture_output=True)
    status_lines = run.stdout.decode("utf-8", errors="replace").splitlines()
    raw = paths["raw"].read_bytes() if paths["raw"].exists() else b""
    status = int(status_lines[0]) if status_lines and status_lines[0].isdigit() else None
    meta = {"source_id": source_id, "requested_url": url,
            "effective_url": status_lines[1] if len(status_lines) > 1 else None,
            "content_type": status_lines[2] if len(status_lines) > 2 else None,
            "started_at_utc": started, "completed_at_utc": utc_now(), "http_status": status,
            "process_returncode": run.returncode, "bytes": len(raw),
            "raw_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
            "raw_path": str(paths["raw"]), "text_path": None, "text_sha256": None,
            "outcome": "failed", "rights": "local_review_only_not_cleared_for_redistribution",
            "capture_method": "single_curl_public_https_no_credentials_no_retry"}
    if run.returncode == 0 and status == 200 and raw and b"%PDF" not in raw[:8]:
        header = raw[:8000].decode("ascii", errors="ignore")
        encoding_match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", header, re.I)
        encoding = encoding_match[1] if encoding_match else "utf-8"
        if encoding.lower() in ("gb2312", "gbk"):
            encoding = "gb18030"
        try:
            document = raw.decode(encoding)
            parser = TextParser()
            parser.feed(document)
            parser.close()
            text = normalize("".join(parser.parts))
            data = {"source_id": source_id, "encoding": encoding, "text": text,
                    "method": "HTMLParser_remove_script_style_noscript_svg_then_whitespace_normalization"}
            write_new(paths["text"], data)
            meta.update(outcome="downloaded_text_extracted", text_path=str(paths["text"]),
                        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), text_char_count=len(text))
        except (UnicodeError, LookupError, ValueError) as exc:
            meta["parse_error"] = type(exc).__name__
    write_new(paths["meta"], meta)
    return meta


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-id", required=True)
    args = parser.parse_args()
    result = capture(args.url, args.out, args.source_id)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result["outcome"] == "downloaded_text_extracted" else 1)
