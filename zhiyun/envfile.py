"""轻量 .env 读取，无额外依赖。

支持两种写法：

    A = 3230xxxxxx; P = your-password;

或标准的每行一个变量：

    ZHIYUN_USERNAME=3230xxxxxx
    ZHIYUN_PASSWORD=your-password

也接受 A / P / ZJU_USERNAME / ZJU_PASSWORD / TOKEN 等别名。
真实环境变量优先，不会被 .env 覆盖。
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from typing import Iterable

from .config import config_dir
from .errors import EnvFileError


CANONICAL_KEYS = {
    "ZHIYUN_USERNAME",
    "ZHIYUN_PASSWORD",
    "ZHIYUN_TOKEN",
    "ZHIYUN_STUDENT_ID",
}

ALIASES = {
    "A": "ZHIYUN_USERNAME",
    "ACCOUNT": "ZHIYUN_USERNAME",
    "USER": "ZHIYUN_USERNAME",
    "USERNAME": "ZHIYUN_USERNAME",
    "STUDENT_ID": "ZHIYUN_STUDENT_ID",
    "ZJU_USERNAME": "ZHIYUN_USERNAME",
    "ZJU_STUDENT_ID": "ZHIYUN_STUDENT_ID",
    "P": "ZHIYUN_PASSWORD",
    "PASS": "ZHIYUN_PASSWORD",
    "PASSWORD": "ZHIYUN_PASSWORD",
    "ZJU_PASSWORD": "ZHIYUN_PASSWORD",
    "TOKEN": "ZHIYUN_TOKEN",
    "JWT": "ZHIYUN_TOKEN",
    "ZJU_TOKEN": "ZHIYUN_TOKEN",
}

ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", re.S
)
SEMICOLON_STYLE_RE = re.compile(
    r";\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=", re.I
)

_warned_paths: set[Path] = set()


def _strip_comment(text: str) -> str:
    """去掉引号外的行内注释（# 前必须是空白）。"""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1].isspace()):
            return text[:index]
    return text


def _split_semicolons(text: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    escaped = False
    for char in text:
        if escaped:
            buffer.append(char)
            escaped = False
            continue
        if char == "\\":
            buffer.append(char)
            escaped = True
            continue
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            buffer.append(char)
        elif char == ";":
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return parts


def _clean_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        quote = value[0]
        value = value[1:-1]
        value = value.replace("\\" + quote, quote).replace("\\\\", "\\")
        return value
    return value.strip()


def _uses_semicolon_style(text: str) -> bool:
    """判断文件是否使用分号作为赋值分隔符。

    典型写法：A = xxx; P = xxx;
    也支持每个赋值单独一行、行尾带分号。普通 .env（每行一个赋值、无行尾分号）
    不会触发，因此密码里的分号会原样保留。
    """
    meaningful: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = _strip_comment(line).strip()
        if not line:
            continue
        meaningful.append(line)
        segments = [seg for seg in _split_semicolons(line) if seg.strip()]
        if len(segments) >= 2 and all(ASSIGNMENT_RE.match(seg) for seg in segments):
            return True

    if len(meaningful) >= 2 and all(line.endswith(";") for line in meaningful):
        return True

    return bool(SEMICOLON_STYLE_RE.search(text)) and len(meaningful) == 1


def parse_env(text: str) -> dict[str, str]:
    """把 .env 文本解析成规范变量名 -> 值。无法识别的键会被忽略。"""
    text = text.lstrip("﻿")
    semicolon_style = _uses_semicolon_style(text)
    values: dict[str, str] = {}
    priorities: dict[str, int] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = _strip_comment(line).strip()
        if not line:
            continue

        segments = _split_semicolons(line) if semicolon_style else [line]
        for segment in segments:
            segment = segment.strip()
            if not segment or segment.startswith("#"):
                continue
            match = ASSIGNMENT_RE.match(segment)
            if not match:
                continue
            key = match.group(1).upper()
            value = _clean_value(match.group(2))
            if not value:
                continue

            if key in CANONICAL_KEYS:
                canonical = key
                priority = 2
            elif key in ALIASES:
                canonical = ALIASES[key]
                priority = 1
            else:
                continue

            if priorities.get(canonical, -1) <= priority:
                values[canonical] = value
                priorities[canonical] = priority

    return values


def load_env_file(path: Path, *, required: bool = False) -> dict[str, str]:
    """读取 .env 并把它认识的变量放进 os.environ（不覆盖已存在的值）。"""
    path = path.expanduser()
    if not path.exists():
        if required:
            raise EnvFileError(f"找不到 .env 文件：{path}")
        return {}
    if not path.is_file():
        if required:
            raise EnvFileError(f"不是普通文件：{path}")
        return {}

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        if required:
            raise EnvFileError(f"无法读取 {path}：{exc}") from exc
        return {}

    parsed = parse_env(text)
    for key, value in parsed.items():
        os.environ.setdefault(key, value)

    if "ZHIYUN_PASSWORD" in parsed and path not in _warned_paths:
        _warned_paths.add(path)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            mode = 0
        if mode & 0o077:
            print(
                f"注意：{path} 里有明文密码，建议执行 chmod 600 {path}",
                file=sys.stderr,
            )

    if required and not parsed:
        print(
            f"注意：{path} 里没有找到 A/P 或 ZHIYUN_* 变量。",
            file=sys.stderr,
        )
    return parsed


def autoload_env_files(explicit: str | None = None) -> list[Path]:
    """加载显式指定的 .env，或自动加载 ./.env 和配置目录下的 .env。"""
    loaded: list[Path] = []

    if explicit:
        path = Path(explicit).expanduser()
        load_env_file(path, required=True)
        return [path]

    env_file = os.environ.get("ZHIYUN_ENV_FILE", "").strip()
    if env_file:
        path = Path(env_file).expanduser()
        load_env_file(path, required=True)
        return [path]

    candidates = [Path.cwd() / ".env", config_dir() / ".env"]
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file():
            load_env_file(candidate)
            loaded.append(candidate)
    return loaded
