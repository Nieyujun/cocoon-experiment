"""离线冻结 index.html 内的五条提示词，不执行 JavaScript 或调用模型。

bundle_sha256 是除自身字段之外的 bundle 的 canonical_hash 内容哈希，
不是 JSON 文件的字节哈希；JSON 排版变化不影响该值。提示词 sha256 则是
反引号之间原始文本的 UTF-8 字节哈希，不去空白、不规范化换行。
"""

import hashlib
import io
import json
import math
import re
import zipfile
from pathlib import Path

from .runtime import canonical_hash, write_json_new


STAGES = ("search", "label", "blindspot", "integrate", "mirror")
_DECLARATION = re.compile(r"(?<![\w$])const[ \t\r\n]+PROMPT_TEMPLATES(?![\w$])")
_KEY = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_NEWLINE = re.compile(r"\r\n|[\r\n]")
_WHITESPACE = " \t\r\n"
_BUNDLE_FIELDS = frozenset({
    "schema_version", "version", "source_entry_sha256", "source_zip_sha256",
    "prompt_source", "prompts", "bundle_sha256",
})
_PROMPT_FIELDS = frozenset({"text", "sha256", "line_start", "line_end"})


def _require_hash(value, name):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"{name} 必须是小写的完整 SHA-256")


def _text_hash(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("提示词必须是非空文本")
    if "\\" in text or "${" in text or "`" in text:
        raise ValueError("提示词不允许转义、插值或内嵌反引号")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_prompts(text: str) -> dict:
    """仅接受唯一 const PROMPT_TEMPLATES = {裸键: `原文`, ...};。

    只搜索声明标识并解析这一受限字面对象，不解释外围 HTML/JS 语义。
    歧义声明也拒绝；不跳过对象内注释、不接受表达式或引号键。
    允许末尾逗号，终止符必须恰为 };。行号为起止反引号所在的
    1 基闭区间，CRLF 计一行，原始文本中的换行保持不变。
    """
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    matches = list(_DECLARATION.finditer(text))
    if len(matches) != 1:
        raise ValueError("必须有且仅有一个 const PROMPT_TEMPLATES 声明")
    position = matches[0].end()

    def skip_space():
        nonlocal position
        while position < len(text) and text[position] in _WHITESPACE:
            position += 1

    def consume(token):
        nonlocal position
        skip_space()
        if not text.startswith(token, position):
            raise ValueError(f"提示词对象结构不受支持，预期 {token}")
        position += len(token)

    consume("=")
    consume("{")
    prompts = {}
    while True:
        skip_space()
        if text.startswith("};", position):
            position += 2
            break
        key = _KEY.match(text, position)
        if key is None:
            raise ValueError("提示词对象仅支持裸 stage 键和模板字面量")
        stage = key.group()
        if stage not in STAGES or stage in prompts:
            raise ValueError("提示词对象包含额外或重复 stage")
        position = key.end()
        consume(":")
        consume("`")
        opening = position - 1
        closing = text.find("`", position)
        if closing == -1:
            raise ValueError("模板字面量未闭合")
        raw = text[position:closing]
        prompts[stage] = {
            "text": raw,
            "sha256": _text_hash(raw),
            "line_start": 1 + len(_NEWLINE.findall(text, 0, opening)),
            "line_end": 1 + len(_NEWLINE.findall(text, 0, closing)),
        }
        position = closing + 1
        skip_space()
        if text.startswith("};", position):
            position += 2
            break
        consume(",")
    if set(prompts) != set(STAGES):
        raise ValueError("提示词对象必须完整包含五个 stage")
    return {stage: prompts[stage] for stage in STAGES}


def _verify_bundle(bundle, expected_sha256=None):
    if not isinstance(bundle, dict) or set(bundle) != _BUNDLE_FIELDS:
        raise ValueError("提示词 bundle 顶层字段不符合协议")
    if (
        bundle["schema_version"] != "1.0"
        or bundle["version"] != "cocoon-original-inline-v1"
        or bundle["prompt_source"] != "index.html:PROMPT_TEMPLATES"
    ):
        raise ValueError("提示词 bundle 版本或来源标识不受支持")
    for field in ("source_entry_sha256", "source_zip_sha256", "bundle_sha256"):
        _require_hash(bundle[field], field)
    prompts = bundle["prompts"]
    if not isinstance(prompts, dict) or set(prompts) != set(STAGES):
        raise ValueError("提示词 bundle 必须完整且仅包含五个 stage")
    for stage in STAGES:
        prompt = prompts[stage]
        if not isinstance(prompt, dict) or set(prompt) != _PROMPT_FIELDS:
            raise ValueError(f"{stage} 提示词字段不符合协议")
        _require_hash(prompt["sha256"], f"{stage}.sha256")
        if _text_hash(prompt["text"]) != prompt["sha256"]:
            raise ValueError(f"{stage} 原始文本哈希不匹配")
        start, end = prompt["line_start"], prompt["line_end"]
        if (
            type(start) is not int or type(end) is not int
            or start < 1 or end < start
            or end - start != len(_NEWLINE.findall(prompt["text"]))
        ):
            raise ValueError(f"{stage} 行号范围无效")
    content = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    if canonical_hash(content) != bundle["bundle_sha256"]:
        raise ValueError("bundle_sha256 内容哈希不匹配")
    if expected_sha256 is not None:
        _require_hash(expected_sha256, "expected_sha256")
        if bundle["bundle_sha256"] != expected_sha256:
            raise ValueError("提示词 bundle 与预期内容哈希不匹配")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 不允许重复字段")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("JSON 不允许非有限数值")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON 不允许溢出为非有限数值")
    return number


def load_prompt_bundle(path: Path, expected_sha256: str | None = None) -> dict:
    """严格读取并校验；expected_sha256 是 bundle 内容哈希而非文件哈希。"""
    data = Path(path).read_bytes()
    try:
        bundle = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except (UnicodeError, ValueError) as exc:
        raise ValueError("提示词 bundle 不是有效的严格 UTF-8 JSON") from exc
    _verify_bundle(bundle, expected_sha256)
    return bundle


def _read_sources(entry_path, zip_path, expected_entry_sha256, expected_zip_sha256):
    _require_hash(expected_entry_sha256, "expected_entry_sha256")
    _require_hash(expected_zip_sha256, "expected_zip_sha256")
    entry = Path(entry_path).read_bytes()
    entry_hash = hashlib.sha256(entry).hexdigest()
    if entry_hash != expected_entry_sha256:
        raise ValueError("index.html 原件 SHA-256 不匹配")
    archive = Path(zip_path).read_bytes()
    archive_hash = hashlib.sha256(archive).hexdigest()
    if archive_hash != expected_zip_sha256:
        raise ValueError("ZIP 原件 SHA-256 不匹配")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            entries = [item for item in source.infolist() if item.filename == "index.html"]
            if len(entries) != 1:
                raise ValueError("ZIP 必须含唯一根目录 index.html")
            member = entries[0]
            if member.file_size != len(entry) or source.read(member) != entry:
                raise ValueError("ZIP index.html 与独立原件字节不相等")
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as exc:
        raise ValueError("ZIP 原件无法安全读取") from exc
    return entry, entry_hash, archive_hash


def freeze_prompts(
    entry_path: Path, zip_path: Path, expected_entry_sha256: str,
    expected_zip_sha256: str, output_path: Path,
) -> dict:
    """先验两份原件哈希及 ZIP 字节一致性，再静态冻结；不覆盖不同内容。"""
    entry, entry_hash, archive_hash = _read_sources(
        entry_path, zip_path, expected_entry_sha256, expected_zip_sha256,
    )
    bundle = {
        "schema_version": "1.0",
        "version": "cocoon-original-inline-v1",
        "source_entry_sha256": entry_hash,
        "source_zip_sha256": archive_hash,
        "prompt_source": "index.html:PROMPT_TEMPLATES",
        "prompts": extract_prompts(entry.decode("utf-8")),
    }
    bundle["bundle_sha256"] = canonical_hash(bundle)
    _verify_bundle(bundle)
    output_path = Path(output_path)
    if not output_path.parent.is_dir():
        raise FileNotFoundError("提示词 bundle 父目录必须已存在")
    try:
        write_json_new(output_path, bundle)
    except FileExistsError:
        existing = load_prompt_bundle(output_path)
        if existing != bundle:
            raise FileExistsError("已有提示词 bundle 内容不同，拒绝覆盖") from None
        return existing
    return bundle


def verify_original_sources(bundle, entry_path, zip_path) -> dict:
    """只读校验内容哈希、原件哈希、ZIP 字节及重新提取的文本和行号。"""
    _verify_bundle(bundle)
    entry, entry_hash, archive_hash = _read_sources(
        entry_path, zip_path, bundle["source_entry_sha256"], bundle["source_zip_sha256"],
    )
    if extract_prompts(entry.decode("utf-8")) != bundle["prompts"]:
        raise ValueError("bundle 提示词或行号与原件不一致")
    return {
        "hashes_verified": True,
        "zip_entry_verified": True,
        "prompts_verified": True,
        "stage_count": len(STAGES),
        "source_entry_sha256": entry_hash,
        "source_zip_sha256": archive_hash,
        "bundle_sha256": bundle["bundle_sha256"],
    }
