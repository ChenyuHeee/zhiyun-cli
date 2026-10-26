"""zhiyun 命令行入口。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from . import __version__
from .api import ZhiyunApi
from .auth import browser_login, jwt_payload, login, normalize_jwt
from .config import (
    CONFIG_FILE,
    CREDENTIALS_FILE,
    Config,
    delete_config,
    load_config,
    load_credentials,
    save_config,
    save_credentials,
)
from .envfile import autoload_env_files
from .errors import ApiError, AuthExpiredError, ExportError, LoginError, ZhiyunError
from .export import ExportOptions, export_lecture, parse_datetime


# ---------------------------------------------------------------------------
# 输出小工具
# ---------------------------------------------------------------------------


def _disp_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _disp_width(text) <= width:
        return text
    out = ""
    used = 0
    for ch in text:
        char_width = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + char_width > width - 1:
            break
        out += ch
        used += char_width
    return out + "…"


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _disp_width(text))


def _print_table(headers: tuple[str, ...], widths: tuple[int, ...], rows: list[tuple[str, ...]]) -> None:
    print("  ".join(_pad(_truncate(cell, width), width) for cell, width in zip(headers, widths)).rstrip())
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(_pad(_truncate(cell, width), width) for cell, width in zip(row, widths)).rstrip())


def _format_expiry(timestamp: int) -> str:
    if not timestamp:
        return "平台未提供过期时间"
    dt = datetime.fromtimestamp(timestamp)
    left = int(timestamp - time.time())
    if left <= 0:
        return f"{dt:%Y-%m-%d %H:%M}（已过期）"
    days = left // 86400
    if days >= 1:
        return f"{dt:%Y-%m-%d %H:%M}（剩余约 {days} 天）"
    hours = max(1, left // 3600)
    return f"{dt:%Y-%m-%d %H:%M}（剩余约 {hours} 小时）"


def _print_auth_summary(cfg: Config) -> None:
    if cfg.username or cfg.student_id:
        print(f"学号：{cfg.username or cfg.student_id}")
    print(f"凭证有效期：{_format_expiry(cfg.expires_at)}")
    if cfg.source:
        print(f"凭证来源：{cfg.source}")
    print(f"配置文件：{CONFIG_FILE}")


def _effective_config() -> Config:
    cfg = load_config()
    env_token_raw = os.environ.get("ZHIYUN_TOKEN", "").strip()
    if env_token_raw:
        token = normalize_jwt(env_token_raw)
        if not token:
            raise AuthExpiredError("环境变量 ZHIYUN_TOKEN 不是有效的 JWT")
        payload = jwt_payload(token)
        cfg.jwt = token
        cfg.source = "env"
        try:
            cfg.expires_at = int(payload.get("exp") or 0)
        except (TypeError, ValueError):
            cfg.expires_at = 0
        env_student = os.environ.get("ZHIYUN_STUDENT_ID", "").strip()
        if env_student:
            cfg.student_id = env_student
            cfg.username = env_student
    return cfg


def _make_api(cfg: Config) -> ZhiyunApi:
    if not cfg.jwt:
        raise AuthExpiredError("尚未登录智云课堂。请先运行 `zhiyun login`。")
    return ZhiyunApi(cfg.jwt, student_id=cfg.student_id, username=cfg.username)


# ---------------------------------------------------------------------------
# 选择课程 / 课时
# ---------------------------------------------------------------------------


def _course_row(course: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(course.get("course_id", "")),
        str(course.get("term") or "—"),
        str(course.get("title") or "—"),
        str(course.get("teacher") or "—"),
    )


def _print_courses(courses: list[dict[str, Any]]) -> None:
    _print_table(
        ("ID", "学期", "课程", "教师"),
        (10, 18, 34, 16),
        [_course_row(course) for course in courses],
    )


def _choose_course(courses: list[dict[str, Any]], prompt: str = "请选择课程") -> dict[str, Any]:
    _print_courses(courses)
    while True:
        raw = input(f"{prompt}（1-{len(courses)}，输入 q 取消）：").strip()
        if raw.lower() in {"q", "quit", "exit"}:
            raise KeyboardInterrupt
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(courses):
                return courses[index - 1]
        print("输入无效，请重试。")


def _resolve_course(
    api: ZhiyunApi,
    query: str | None,
    *,
    interactive: bool = True,
) -> dict[str, Any]:
    try:
        courses = api.my_courses()
    except AuthExpiredError:
        raise
    except ApiError as exc:
        if query and query.strip().isdigit():
            course_id = query.strip()
            return {
                "course_id": course_id,
                "title": f"课程 {course_id}",
                "teacher": "",
                "term": "",
                "college": "",
                "course_code": "",
            }
        raise ApiError(f"无法获取课程列表：{exc}") from exc

    if not query:
        if not courses:
            raise ZhiyunError("当前账号没有可访问的课程。你可以直接用课程 ID 尝试，例如 `zhiyun export 123456 --lecture last`。")
        if len(courses) == 1:
            return courses[0]
        if not interactive or not sys.stdin.isatty():
            lines = "\n".join(
                f"  {c['course_id']}  {c['title']}（{c.get('teacher') or '未知教师'}）"
                for c in courses[:15]
            )
            raise ZhiyunError(f"请指定课程名或课程 ID。可用课程：\n{lines}")
        return _choose_course(courses)

    raw_query = query.strip()
    if raw_query.isdigit():
        for course in courses:
            if str(course.get("course_id")) == raw_query:
                return course
        return {
            "course_id": raw_query,
            "title": f"课程 {raw_query}",
            "teacher": "",
            "term": "",
            "college": "",
            "course_code": "",
        }

    needle = raw_query.casefold()
    exact = [c for c in courses if str(c.get("title", "")).casefold() == needle]
    if len(exact) == 1:
        return exact[0]

    matches = [
        c
        for c in courses
        if needle in str(c.get("title", "")).casefold()
        or needle in str(c.get("teacher", "")).casefold()
        or needle in str(c.get("term", "")).casefold()
        or needle in str(c.get("course_code", "")).casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ZhiyunError(f"没有找到匹配「{raw_query}」的课程。先用 `zhiyun courses` 看看课程列表。")
    if interactive and sys.stdin.isatty():
        return _choose_course(matches, prompt="匹配到多门课程，请选择")
    lines = "\n".join(
        f"  {c['course_id']}  {c['title']}（{c.get('teacher') or '未知教师'}）"
        for c in matches[:15]
    )
    more = f"\n  …还有 {len(matches) - 15} 门" if len(matches) > 15 else ""
    raise ZhiyunError(f"匹配到多门课程，请用课程 ID 指定：\n{lines}{more}")


def _lecture_row(lecture: dict[str, Any]) -> tuple[str, ...]:
    start = parse_datetime(lecture.get("start_at"))
    status = "有字幕" if lecture.get("has_transcript") else "无字幕"
    return (
        str(lecture.get("index", "")),
        f"{start:%Y-%m-%d}" if start else "—",
        str(lecture.get("title") or "—"),
        status,
        str(lecture.get("sub_id", "")),
    )


def _print_lectures(course: dict[str, Any], lectures: list[dict[str, Any]]) -> None:
    print(f"课程：{course.get('title', '')}（ID {course.get('course_id', '')}）")
    if course.get("teacher"):
        print(f"教师：{course['teacher']}")
    print()
    _print_table(
        ("#", "日期", "课时", "状态", "sub_id"),
        (4, 10, 36, 8, 10),
        [_lecture_row(lecture) for lecture in lectures],
    )
    if any(not lecture.get("has_transcript") for lecture in lectures):
        print()
        print("提示：标记为「无字幕」的课时可能还没有生成回放转写；导出时可加 --try-transcript 强制尝试。")


def _choose_lecture(lectures: list[dict[str, Any]]) -> dict[str, Any]:
    _print_table(
        ("#", "日期", "课时", "状态", "sub_id"),
        (4, 10, 36, 8, 10),
        [_lecture_row(lecture) for lecture in lectures],
    )
    print()
    available = [item for item in lectures if item.get("has_transcript")]
    default = (available or lectures)[-1]
    while True:
        raw = input(
            f"选择课时（1-{len(lectures)} 或 sub_id，回车=最新 {default.get('index')}）："
        ).strip()
        if raw.lower() in {"q", "quit", "exit"}:
            raise KeyboardInterrupt
        if not raw:
            return default
        if raw.isdigit():
            number = int(raw)
            for lecture in lectures:
                if int(lecture.get("index", 0)) == number:
                    return lecture
            for lecture in lectures:
                if str(lecture.get("sub_id")) == raw:
                    return lecture
        needle = raw.casefold()
        matches = [item for item in lectures if needle in str(item.get("title", "")).casefold()]
        if len(matches) == 1:
            return matches[0]
        print("输入无效，请重试。")


def _select_lecture(
    lectures: list[dict[str, Any]],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    flags = [bool(args.all), bool(args.lecture), bool(args.sub_id), bool(args.title)]
    if sum(flags) > 1:
        raise ZhiyunError("--all、--lecture、--sub-id、--title 只能同时使用一个。")

    if args.all:
        available = [item for item in lectures if item.get("has_transcript")]
        if available:
            skipped = len(lectures) - len(available)
            if skipped:
                print(f"跳过 {skipped} 节没有字幕的课时（共 {len(lectures)} 节）。")
            return available
        print("没有课时标记为有字幕，将尝试导出全部课时。")
        return lectures

    if args.sub_id:
        sub_id = str(args.sub_id).strip()
        for lecture in lectures:
            if str(lecture.get("sub_id")) == sub_id:
                return [lecture]
        return [
            {
                "course_id": "",
                "sub_id": sub_id,
                "title": f"课时 {sub_id}",
                "status": "6",
                "start_at": "",
                "end_at": "",
                "duration": 0,
                "lecturer": "",
                "playback_url": "",
                "has_transcript": True,
                "index": 0,
            }
        ]

    if args.title:
        needle = str(args.title).strip().casefold()
        matches = [item for item in lectures if needle in str(item.get("title", "")).casefold()]
        if not matches:
            raise ZhiyunError(f"没有找到标题包含「{args.title}」的课时。")
        if len(matches) > 1:
            lines = "\n".join(f"  #{m.get('index')}  {m.get('title')}（{m.get('sub_id')}）" for m in matches)
            raise ZhiyunError(f"标题匹配到多节课，请改用 --lecture 序号或 --sub-id：\n{lines}")
        return matches

    if args.lecture:
        value = str(args.lecture).strip()
        if value.lower() in {"last", "latest", "最新"}:
            available = [item for item in lectures if item.get("has_transcript")] or lectures
            return [available[-1]]
        if value.isdigit():
            number = int(value)
            for lecture in lectures:
                if int(lecture.get("index", 0)) == number:
                    return [lecture]
            for lecture in lectures:
                if str(lecture.get("sub_id")) == value:
                    return [lecture]
        raise ZhiyunError(f"无效的课时序号：{value}。先用 `zhiyun lectures` 查看序号，或使用 --sub-id。")

    if not lectures:
        raise ZhiyunError("这门课没有可导出的课时。")
    if not sys.stdin.isatty():
        raise ZhiyunError(
            "请用 --lecture N、--sub-id 或 --all 指定要导出的课时；"
            "也可以先运行 `zhiyun lectures <课程>` 查看。"
        )
    return [_choose_lecture(lectures)]


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> int:
    if args.token and args.token_stdin:
        raise LoginError("--token 和 --token-stdin 只能选一个。")
    if args.browser and (args.token or args.token_stdin):
        raise LoginError("--browser 不能和 --token 一起使用。")
    if args.save_password and (args.browser or args.token or args.token_stdin):
        raise LoginError("--save-password 只能用于学号密码登录。")

    if args.token or args.token_stdin:
        raw = args.token if args.token else sys.stdin.read()
        token = normalize_jwt(raw)
        if not token:
            raise LoginError("没有从输入中解析到有效的 JWT。")
        payload = jwt_payload(token)
        student_id = (
            args.student_id
            or os.environ.get("ZHIYUN_STUDENT_ID", "").strip()
            or str(payload.get("user_name") or payload.get("name") or "")
        )
        cfg = Config(
            jwt=token,
            username=student_id,
            student_id=student_id,
            expires_at=_int_or_zero(payload.get("exp")),
            saved_at=int(time.time()),
            source="manual",
        )
        save_config(cfg)
        print("智云凭证已保存。")
        _print_auth_summary(cfg)
        return _verify_login(cfg)

    cfg = load_config()
    creds = load_credentials()
    env_password = os.environ.get("ZHIYUN_PASSWORD", "")

    # 已有有效凭证时，不重复登录（--force 除外）。
    if (
        cfg.jwt
        and not cfg.is_expired()
        and not args.force
        and not args.browser
        and not args.username
        and not args.password_stdin
        and not args.save_password
    ):
        print("当前智云凭证仍在有效期内，无需重新登录。")
        _print_auth_summary(cfg)
        print("如需强制重新登录，请加 --force。")
        return 0

    if args.browser:
        new_cfg = browser_login(timeout=args.timeout)
    else:
        username = (
            args.username
            or os.environ.get("ZHIYUN_USERNAME", "").strip()
            or cfg.username
            or cfg.student_id
            or creds.get("username", "")
        )
        if not username:
            if not sys.stdin.isatty():
                raise LoginError(
                    "非交互环境请设置 ZHIYUN_USERNAME / ZHIYUN_PASSWORD，"
                    "或使用 `zhiyun login --browser`。"
                )
            username = input("学号/统一认证用户名：").strip()
        if not username:
            raise LoginError("学号不能为空。")

        password = env_password or (
            creds.get("password", "") if creds.get("username") == username else ""
        )
        if not password:
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\r\n")
            elif sys.stdin.isatty():
                password = getpass.getpass("密码（输入时不显示）：")
            else:
                raise LoginError(
                    "非交互环境请设置 ZHIYUN_PASSWORD，使用 --password-stdin，"
                    "或使用 `zhiyun login --browser`。"
                )
        if not password:
            raise LoginError("密码不能为空。")

        new_cfg = login(username, password)
        if args.save_password:
            save_credentials(username, password)
            print(f"账号密码已保存到 {CREDENTIALS_FILE}（权限 0600，仅本机可见）。")

    if not new_cfg.student_id:
        new_cfg.student_id = (
            args.student_id
            or os.environ.get("ZHIYUN_STUDENT_ID", "").strip()
            or cfg.student_id
            or cfg.username
        )
    if not new_cfg.username:
        new_cfg.username = new_cfg.student_id or cfg.username

    save_config(new_cfg)
    print("登录成功，智云凭证已保存。")
    _print_auth_summary(new_cfg)
    return _verify_login(new_cfg)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _verify_login(cfg: Config) -> int:
    print("正在验证凭证…")
    try:
        api = _make_api(cfg)
        courses = api.my_courses()
    except AuthExpiredError as exc:
        print(f"警告：凭证已保存，但验证失败：{exc}", file=sys.stderr)
        print("可尝试 `zhiyun login --browser --force`。", file=sys.stderr)
        return 2
    except (ZhiyunError, requests.RequestException) as exc:
        print(f"警告：凭证已保存，但验证时出错：{exc}", file=sys.stderr)
        return 1
    if courses:
        print(f"验证通过：当前账号可访问 {len(courses)} 门课程。")
    else:
        print("验证通过（当前账号课程列表为空）。")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _effective_config()
    logged_in = bool(cfg.jwt)
    expired = cfg.is_expired()

    if args.json:
        data = {
            "logged_in": logged_in,
            "username": cfg.username,
            "student_id": cfg.student_id,
            "source": cfg.source,
            "expires_at": cfg.expires_at or None,
            "expired": expired,
            "token": cfg.masked_token(),
        }
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"登录状态：{'已登录' if logged_in else '未登录'}")
        if logged_in:
            _print_auth_summary(cfg)
        else:
            print("提示：运行 `zhiyun login` 登录。")

    if not logged_in:
        return 1

    if args.check:
        print("正在检查接口权限…")
        try:
            api = _make_api(cfg)
            courses = api.my_courses()
        except AuthExpiredError as exc:
            print(f"接口验证失败：{exc}", file=sys.stderr)
            return 2
        except (ZhiyunError, requests.RequestException) as exc:
            print(f"接口验证出错：{exc}", file=sys.stderr)
            return 1
        print(f"接口验证通过：可访问 {len(courses)} 门课程。")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    delete_config(keep_credentials=args.keep_credentials)
    print("已清除本地智云凭证。")
    if args.keep_credentials:
        print("已保留工具保存的账号密码。")
    else:
        print("工具保存的账号密码也已清除。")
    print("项目里的 .env 文件不会被删除；如果里面有密码，请自行处理。")
    return 0


def cmd_courses(args: argparse.Namespace) -> int:
    cfg = _effective_config()
    api = _make_api(cfg)
    courses = api.my_courses()
    keyword = (args.keyword or "").strip()
    if keyword:
        needle = keyword.casefold()
        courses = [
            course
            for course in courses
            if needle in str(course.get("title", "")).casefold()
            or needle in str(course.get("teacher", "")).casefold()
            or needle in str(course.get("term", "")).casefold()
            or needle in str(course.get("college", "")).casefold()
            or needle in str(course.get("course_code", "")).casefold()
        ]

    if args.json:
        print(json.dumps(courses, ensure_ascii=False, indent=2))
        return 0
    if not courses:
        if keyword:
            print(f"没有找到包含「{keyword}」的课程。")
        else:
            print("没有可访问的课程。")
        return 0
    _print_courses(courses)
    return 0


def cmd_lectures(args: argparse.Namespace) -> int:
    cfg = _effective_config()
    api = _make_api(cfg)
    course = _resolve_course(api, args.course, interactive=not args.json)
    lectures = api.lectures(course["course_id"])
    if args.available:
        lectures = [lecture for lecture in lectures if lecture.get("has_transcript")]

    if args.json:
        print(json.dumps(lectures, ensure_ascii=False, indent=2))
        return 0
    if not lectures:
        print("没有符合条件的课时。")
        return 0
    _print_lectures(course, lectures)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    if args.flat and args.all:
        raise ZhiyunError("--flat 只能用于导出单节课，不能和 --all 一起使用。")
    if args.transcript_only and args.slides_only:
        raise ZhiyunError("--transcript-only 和 --slides-only 不能同时使用。")

    cfg = _effective_config()
    api = _make_api(cfg)
    course = _resolve_course(api, args.course, interactive=True)
    lectures = api.lectures(course["course_id"])
    selected = _select_lecture(lectures, args)

    base_dir = (
        Path(args.output).expanduser()
        if args.output
        else (Path.cwd() / "zhiyun_output")
    ).resolve()
    opts = ExportOptions(
        base_dir=base_dir,
        force=args.force,
        images=not (args.no_images or args.transcript_only),
        save_raw=args.save_raw,
        flat=args.flat,
        transcript_only=args.transcript_only,
        slides_only=args.slides_only,
        try_transcript=args.try_transcript,
    )

    def log(message: str) -> None:
        if not args.quiet:
            print(message)

    failures: list[tuple[dict[str, Any], Exception]] = []
    total = len(selected)
    for number, lecture in enumerate(selected, 1):
        print(f"[{number}/{total}] {course.get('title', '')} · {lecture.get('title', '')}")
        try:
            result = export_lecture(api, course, lecture, opts, log=log)
        except AuthExpiredError:
            raise
        except Exception as exc:
            failures.append((lecture, exc))
            print(f"  导出失败：{exc}", file=sys.stderr)
            if total == 1:
                raise
            continue

        print(f"  输出目录：{result.out_dir}")
        print(
            f"  已导出：字幕 {result.transcript_count} 段，"
            f"课件 {result.slide_count} 张"
            f"（图片可用 {result.image_count} 张，本次下载 {result.new_image_count} 张）"
        )
        if result.lecture_md:
            print(f"  合并稿：{result.lecture_md}")
        for note in result.notes[:6]:
            print(f"  提示：{note}")
        if len(result.notes) > 6:
            print(f"  提示：另有 {len(result.notes) - 6} 条提示，详见 metadata.json")
        print()

    if total > 1:
        success = total - len(failures)
        print(f"完成：成功 {success} / {total} 节。")
        for lecture, exc in failures:
            print(f"  失败：{lecture.get('title', '')}（{lecture.get('sub_id', '')}）：{exc}", file=sys.stderr)
        return 0 if not failures else 1
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zhiyun",
        description="浙江大学智云课堂回放导出：字幕文字稿 + 课件图片。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env-file",
        help="从指定 .env 读取账号密码/Token（默认自动读取 ./.env）",
    )

    p_login = subparsers.add_parser(
        "login", parents=[common], help="登录统一身份认证并保存智云凭证"
    )
    p_login.add_argument("-u", "--username", help="学号/统一认证用户名")
    p_login.add_argument(
        "--browser",
        action="store_true",
        help="打开浏览器人工登录（验证码/扫码/自动登录失败时使用）",
    )
    p_login.add_argument(
        "--token",
        help="直接设置智云 JWT（也可用 ZHIYUN_TOKEN 环境变量；注意 shell 历史）",
    )
    p_login.add_argument(
        "--token-stdin",
        action="store_true",
        help="从标准输入读取智云 JWT",
    )
    p_login.add_argument(
        "--password-stdin",
        action="store_true",
        help="从标准输入读取密码（适合脚本；也可用 ZHIYUN_PASSWORD）",
    )
    p_login.add_argument(
        "--save-password",
        action="store_true",
        help="把账号密码保存到本地（0600，仅建议在个人电脑使用）",
    )
    p_login.add_argument("--student-id", help="手动指定学号（浏览器/Token 登录时可选）")
    p_login.add_argument("--timeout", type=int, default=300, help="浏览器登录等待秒数，默认 300")
    p_login.add_argument("--force", action="store_true", help="即使已有有效凭证也重新登录")
    p_login.set_defaults(func=cmd_login)

    p_status = subparsers.add_parser(
        "status", parents=[common], help="查看本地登录状态"
    )
    p_status.add_argument("--check", action="store_true", help="调用接口验证凭证是否可用")
    p_status.add_argument("--json", action="store_true", help="输出 JSON")
    p_status.set_defaults(func=cmd_status)

    p_logout = subparsers.add_parser(
        "logout", parents=[common], help="清除本地登录信息"
    )
    p_logout.add_argument(
        "--keep-credentials",
        action="store_true",
        help="保留已保存的账号密码，只清除 JWT",
    )
    p_logout.set_defaults(func=cmd_logout)

    p_courses = subparsers.add_parser(
        "courses",
        aliases=["course"],
        parents=[common],
        help="列出我的课程",
    )
    p_courses.add_argument("--keyword", help="按课程名/教师/学期筛选")
    p_courses.add_argument("--json", action="store_true", help="输出 JSON")
    p_courses.set_defaults(func=cmd_courses)

    p_lectures = subparsers.add_parser(
        "lectures",
        aliases=["lecture"],
        parents=[common],
        help="列出某门课的课时",
    )
    p_lectures.add_argument("course", nargs="?", help="课程名/关键字/课程 ID；省略则交互选择")
    p_lectures.add_argument("--available", action="store_true", help="只显示有字幕的课时")
    p_lectures.add_argument("--json", action="store_true", help="输出 JSON")
    p_lectures.set_defaults(func=cmd_lectures)

    p_export = subparsers.add_parser(
        "export",
        aliases=["download"],
        parents=[common],
        help="导出某节课的文字稿和课件图片",
    )
    p_export.add_argument("course", nargs="?", help="课程名/关键字/课程 ID；省略则交互选择")
    p_export.add_argument(
        "--lecture",
        help="课时序号（从 lectures 查看），或 last 表示最新有字幕课时",
    )
    p_export.add_argument("--sub-id", help="直接指定课时 sub_id")
    p_export.add_argument("--title", help="按课时标题关键字选择")
    p_export.add_argument("--all", action="store_true", help="导出该课程所有有字幕的课时")
    p_export.add_argument(
        "-o",
        "--output",
        help="输出根目录（默认 ./zhiyun_output）",
    )
    p_export.add_argument(
        "--flat",
        action="store_true",
        help="不创建课程/课时子目录，直接输出到 -o 指定的目录",
    )
    p_export.add_argument("--force", action="store_true", help="重新下载已存在的图片/覆盖输出")
    p_export.add_argument("--no-images", action="store_true", help="不下载课件图片")
    p_export.add_argument("--transcript-only", action="store_true", help="只导出字幕，不抓课件")
    p_export.add_argument("--slides-only", action="store_true", help="只导出课件，不抓字幕")
    p_export.add_argument(
        "--try-transcript",
        action="store_true",
        help="即使课时未标记为有字幕，也尝试请求字幕",
    )
    p_export.add_argument("--save-raw", action="store_true", help="保存接口原始 JSON 到 raw/")
    p_export.add_argument("--quiet", action="store_true", help="减少进度输出")
    p_export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        autoload_env_files(getattr(args, "env_file", None))
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except LoginError as exc:
        print(f"登录失败：{exc}", file=sys.stderr)
        print("提示：可尝试 `zhiyun login --browser` 人工登录。", file=sys.stderr)
        return 2
    except AuthExpiredError as exc:
        print(f"凭证问题：{exc}", file=sys.stderr)
        print("提示：运行 `zhiyun login` 重新登录。", file=sys.stderr)
        return 2
    except (ExportError, ZhiyunError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"网络错误：{exc}", file=sys.stderr)
        print("提示：检查校园网/网络连接后再试。", file=sys.stderr)
        return 1
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
