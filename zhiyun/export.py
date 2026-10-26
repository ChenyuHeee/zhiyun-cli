"""把一节课导出为文字稿、课件图片和按时间轴合并的 Markdown。"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .api import ZhiyunApi
from .errors import ApiError, AuthExpiredError, ExportError


Log = Callable[[str], None]


@dataclass
class ExportOptions:
    base_dir: Path
    force: bool = False
    images: bool = True
    save_raw: bool = False
    flat: bool = False
    transcript_only: bool = False
    slides_only: bool = False
    try_transcript: bool = False


@dataclass
class ExportResult:
    out_dir: Path
    lecture: dict[str, Any]
    transcript_count: int
    slide_count: int
    image_count: int
    new_image_count: int
    lecture_md: Path | None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 格式化与文件名
# ---------------------------------------------------------------------------


def fmt_clock(total_seconds: Any, *, always_hours: bool = True) -> str:
    try:
        seconds = max(0, int(float(total_seconds or 0)))
    except (TypeError, ValueError):
        seconds = 0
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if always_hours or hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def fmt_file_time(total_seconds: Any) -> str:
    return fmt_clock(total_seconds).replace(":", "-")


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d{9,17}", text):
            number = int(text)
            if number >= 10**14:
                timestamp = number / 1_000_000
            elif number >= 10**11:
                timestamp = number / 1_000
            else:
                timestamp = float(number)
        else:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            return dt
    try:
        return datetime.fromtimestamp(timestamp)
    except (OverflowError, OSError, ValueError):
        return None


def safe_name(value: Any, *, fallback: str = "untitled", max_len: int = 64) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .") or fallback
    if text.upper() in {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        text = "_" + text
    return text


def _lecture_folder_name(lecture: dict[str, Any]) -> str:
    title = str(lecture.get("title") or "").strip()
    title = re.sub(r"^\d{4}[-./年]\s*\d{1,2}[-./月]\s*\d{1,2}日?\s*", "", title).strip()
    title = safe_name(title, fallback=f"课时{lecture.get('sub_id', '')}", max_len=52)
    start = parse_datetime(lecture.get("start_at"))
    if start:
        return safe_name(f"{start:%Y-%m-%d}_{title}", max_len=64)
    return title


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text.rstrip() + "\n")
    os.replace(tmp, path)


def _write_json(path: Path, data: Any) -> None:
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _public_slide(slide: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": slide.get("index"),
        "slide_id": slide.get("slide_id", ""),
        "time": slide.get("time", 0),
        "time_label": fmt_clock(slide.get("time", 0)),
        "title": slide.get("title", ""),
        "url": slide.get("url", ""),
        "file": slide.get("file", ""),
        **({"error": slide["error"]} if slide.get("error") else {}),
    }


# ---------------------------------------------------------------------------
# 文本整理
# ---------------------------------------------------------------------------


def _is_cjk(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


def _join_text(prev: str, nxt: str) -> str:
    if not prev:
        return nxt
    if not nxt:
        return prev
    if nxt[0] in "，。！？；：、,.!?;:":
        return prev + nxt
    if prev[-1] in "，。！？；：、,.!?;:":
        return prev + nxt
    if _is_cjk(prev[-1]) and _is_cjk(nxt[0]):
        return prev + nxt
    return prev + " " + nxt


def make_paragraphs(
    segments: list[dict[str, Any]],
    *,
    gap_seconds: int = 6,
    max_seconds: int = 45,
    max_chars: int = 420,
) -> list[dict[str, Any]]:
    """把逐句字幕合并成适合阅读的段落。

    - 停顿超过 gap_seconds 时另起一段；
    - 一段持续超过 max_seconds 或字数超过 max_chars 时，在下一个句末另起一段。
    """
    paragraphs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for segment in segments:
        if current is None:
            current = {
                "start": segment["start"],
                "end": segment["end"],
                "text": segment["text"],
            }
            continue

        gap = segment["start"] - current["end"]
        span = segment["start"] - current["start"]
        ends_sentence = current["text"].endswith(("。", "！", "？", "!", "?", "."))
        should_break = gap > gap_seconds or (
            ends_sentence and (span >= max_seconds or len(current["text"]) >= max_chars)
        )
        if should_break:
            paragraphs.append(current)
            current = {
                "start": segment["start"],
                "end": segment["end"],
                "text": segment["text"],
            }
        else:
            current["text"] = _join_text(current["text"], segment["text"])
            current["end"] = max(current["end"], segment["end"])
    if current:
        paragraphs.append(current)
    return paragraphs


def _lecture_time_label(lecture: dict[str, Any]) -> str:
    start = parse_datetime(lecture.get("start_at"))
    end = parse_datetime(lecture.get("end_at"))
    if not start:
        return ""
    if end:
        return f"{start:%Y-%m-%d %H:%M}–{end:%H:%M}"
    return f"{start:%Y-%m-%d %H:%M}"


def _header(
    course: dict[str, Any],
    lecture: dict[str, Any],
    *,
    transcript_count: int,
    slide_count: int,
) -> list[str]:
    lines = [
        f"# {course.get('title', '未知课程')} · {lecture.get('title', '未知课时')}",
        "",
    ]
    meta = []
    if course.get("teacher"):
        meta.append(f"- 教师：{course['teacher']}")
    if course.get("term"):
        meta.append(f"- 学期：{course['term']}")
    time_label = _lecture_time_label(lecture)
    if time_label:
        meta.append(f"- 时间：{time_label}")
    meta.append(f"- 课时 ID：{lecture.get('sub_id', '')}")
    meta.append(f"- 字幕片段：{transcript_count}")
    meta.append(f"- 课件图片：{slide_count}")
    meta.append(f"- 导出时间：{datetime.now():%Y-%m-%d %H:%M}")
    lines.extend(meta)
    lines.extend(
        [
            "",
            "> 本文件由 zhiyun-cli 从智云课堂导出，仅供个人学习使用，请勿公开传播。",
            "",
        ]
    )
    return lines


def _transcript_markdown(
    course: dict[str, Any],
    lecture: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    slide_count: int = 0,
) -> str:
    paragraphs = make_paragraphs(segments)
    lines = _header(
        course,
        lecture,
        transcript_count=len(segments),
        slide_count=slide_count,
    )
    lines.append("## 文字稿")
    lines.append("")
    if not paragraphs:
        lines.append("（该课时没有可用的语音识别字幕。）")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    for paragraph in paragraphs:
        lines.append(f"**[{fmt_clock(paragraph['start'])}]** {paragraph['text']}")
        lines.append("")

    lines.extend(
        [
            "## 逐句时间轴",
            "",
            "<details>",
            "<summary>展开逐句时间轴</summary>",
            "",
        ]
    )
    for segment in segments:
        lines.append(f"- [{fmt_clock(segment['start'])}] {segment['text']}")
    lines.extend(["", "</details>", ""])
    return "\n".join(lines).rstrip() + "\n"


def _transcript_plain(segments: list[dict[str, Any]]) -> str:
    paragraphs = make_paragraphs(segments)
    return "\n\n".join(p["text"] for p in paragraphs).rstrip() + "\n"


def _lecture_markdown(
    course: dict[str, Any],
    lecture: dict[str, Any],
    segments: list[dict[str, Any]],
    slides: list[dict[str, Any]],
) -> str:
    lines = _header(
        course,
        lecture,
        transcript_count=len(segments),
        slide_count=sum(1 for slide in slides if slide.get("file")),
    )
    lines.extend(["## 时间轴", ""])

    events: list[tuple[int, int, str, dict[str, Any]]] = []
    for slide in slides:
        events.append((int(slide.get("time") or 0), 0, "slide", slide))
    for segment in segments:
        events.append((int(segment.get("start") or 0), 1, "text", segment))
    events.sort(key=lambda item: (item[0], item[1]))

    if not events:
        lines.append("（这节课没有抓取到字幕或课件。）")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    for time_value, _, kind, item in events:
        if kind == "slide":
            heading = f"### [{fmt_clock(time_value)}] 课件 {item.get('index')}"
            if item.get("title"):
                heading += f" · {item['title']}"
            lines.append(heading)
            lines.append("")
            if item.get("file"):
                lines.append(f"![课件 {item.get('index')}]({item['file']})")
            else:
                lines.append("（课件图片未下载）")
            lines.append("")
        else:
            lines.append(f"- [{fmt_clock(time_value)}] {item.get('text', '')}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def _resolve_out_dir(
    opts: ExportOptions,
    course: dict[str, Any],
    lecture: dict[str, Any],
) -> Path:
    sub_id = str(lecture.get("sub_id", ""))
    if opts.flat:
        if opts.base_dir.exists():
            meta = _read_json(opts.base_dir / "metadata.json")
            existing_sub_id = str(meta.get("lecture", {}).get("sub_id", "")) if meta else ""
            if existing_sub_id and existing_sub_id != sub_id:
                raise ExportError(
                    f"--flat 指定的目录已经导出了另一节课（sub_id={existing_sub_id}），"
                    "请换一个目录，或去掉 --flat。"
                )
        return opts.base_dir

    course_dir = opts.base_dir / safe_name(
        course.get("title") or f"课程{course.get('course_id', '')}",
        fallback="course",
        max_len=48,
    )
    base_name = _lecture_folder_name(lecture)
    candidate = course_dir / base_name
    if candidate.exists():
        meta = _read_json(candidate / "metadata.json")
        existing_sub_id = str(meta.get("lecture", {}).get("sub_id", "")) if meta else ""
        if existing_sub_id == sub_id:
            return candidate
        try:
            has_files = any(candidate.iterdir())
        except OSError:
            has_files = False
        if has_files:
            return course_dir / f"{base_name}_{sub_id}"
    return candidate


def _download_images(
    api: ZhiyunApi,
    slides: list[dict[str, Any]],
    out_dir: Path,
    opts: ExportOptions,
    log: Log,
) -> tuple[int, int, list[str]]:
    if not slides or not opts.images:
        for slide in slides:
            slide["file"] = ""
        return 0, 0, []

    slides_dir = out_dir / "slides"
    slides_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    referer = f"https://classroom.zju.edu.cn/livingroom?sub_id={slides[0].get('sub_id', '')}"
    completed = 0
    available = 0
    new_downloads = 0

    def task(slide: dict[str, Any]) -> tuple[dict[str, Any], Path | None, bool]:
        stem = slides_dir / f"{int(slide.get('index') or 1):03d}_at_{fmt_file_time(slide.get('time', 0))}"
        path, newly_downloaded = api.download_image(
            slide.get("url", ""),
            stem,
            referer=referer,
            force=opts.force,
        )
        return slide, path, newly_downloaded

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(task, slide): slide for slide in slides}
        for future in as_completed(futures):
            slide = futures[future]
            try:
                _, path, newly_downloaded = future.result()
                if path:
                    slide["file"] = path.relative_to(out_dir).as_posix()
                    available += 1
                    if newly_downloaded:
                        new_downloads += 1
                else:
                    slide["file"] = ""
                    notes.append(f"课件 {slide.get('index')} 没有图片地址")
            except AuthExpiredError:
                raise
            except Exception as exc:  # 单张图片失败不阻断整节课
                slide["file"] = ""
                slide["error"] = str(exc)
                notes.append(f"课件 {slide.get('index')} 下载失败：{exc}")
            completed += 1
            if completed == 1 or completed % 10 == 0 or completed == len(slides):
                log(
                    f"  课件图片 {completed}/{len(slides)} 已处理"
                    f"（可用 {available}，本次下载 {new_downloads}）"
                )

    return available, new_downloads, notes


def export_lecture(
    api: ZhiyunApi,
    course: dict[str, Any],
    lecture: dict[str, Any],
    opts: ExportOptions,
    *,
    log: Log = print,
) -> ExportResult:
    notes: list[str] = []
    sub_id = str(lecture.get("sub_id", ""))

    segments: list[dict[str, Any]] = []
    slides: list[dict[str, Any]] = []

    if not opts.slides_only:
        if lecture.get("has_transcript") or opts.try_transcript or opts.transcript_only:
            try:
                segments = api.transcript(sub_id)
                log(f"  字幕：{len(segments)} 段")
            except AuthExpiredError:
                raise
            except ApiError as exc:
                notes.append(f"字幕获取失败：{exc}")
                log(f"  字幕获取失败：{exc}")
        else:
            notes.append("该课时未标记为有字幕，已跳过（可用 --try-transcript 强制尝试）")
            log("  该课时未标记为有字幕，跳过字幕（--try-transcript 可强制尝试）")

    if not opts.transcript_only:
        try:
            slides = api.slide_timeline(str(course.get("course_id", "")), sub_id)
            for slide in slides:
                slide["sub_id"] = sub_id
            log(f"  课件：{len(slides)} 张")
        except AuthExpiredError:
            raise
        except ApiError as exc:
            notes.append(f"课件时间轴获取失败：{exc}")
            log(f"  课件获取失败：{exc}")

    out_dir = _resolve_out_dir(opts, course, lecture)
    out_dir.mkdir(parents=True, exist_ok=True)

    image_count, new_image_count, image_notes = _download_images(
        api, slides, out_dir, opts, log
    )
    notes.extend(image_notes)

    if not segments and not slides:
        log("  这节课没有字幕也没有课件数据。")
    elif not opts.images and slides:
        notes.append("按 --no-images 跳过了课件图片下载")
    elif image_count and new_image_count == 0:
        notes.append("课件图片均已存在，本次未重复下载（--force 可强制重下）")

    transcript_md: Path | None = None
    lecture_md: Path | None = None

    if segments:
        content = _transcript_markdown(
            course, lecture, segments, slide_count=len(slides)
        )
        transcript_md = out_dir / "transcript.md"
        _write_text(transcript_md, content)
        _write_text(out_dir / "transcript.txt", _transcript_plain(segments))
        _write_json(
            out_dir / "transcript.json",
            [
                {
                    "start": segment["start"],
                    "end": segment["end"],
                    "start_label": fmt_clock(segment["start"]),
                    "text": segment["text"],
                }
                for segment in segments
            ],
        )

    if slides:
        _write_json(
            out_dir / "slides.json",
            [_public_slide(slide) for slide in slides],
        )

    lecture_md = out_dir / "lecture.md"
    _write_text(out_dir / "lecture.md", _lecture_markdown(course, lecture, segments, slides))

    metadata = {
        "tool": "zhiyun-cli",
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "course": {
            key: course.get(key)
            for key in ("course_id", "title", "teacher", "term", "college", "course_code")
        },
        "lecture": {
            key: lecture.get(key)
            for key in (
                "sub_id", "title", "status", "start_at", "end_at", "duration",
                "lecturer", "has_transcript", "playback_url", "index",
            )
        },
        "counts": {
            "transcript_segments": len(segments),
            "slides": len(slides),
            "images_available": image_count,
            "images_downloaded_now": new_image_count,
        },
        "slides": [_public_slide(slide) for slide in slides],
        "notes": notes,
    }
    _write_json(out_dir / "metadata.json", metadata)

    if opts.save_raw:
        raw_dir = out_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        if api.last_raw_transcript is not None:
            _write_json(raw_dir / "transcript.json", api.last_raw_transcript)
        if api.last_raw_slides:
            _write_json(raw_dir / "slides.json", api.last_raw_slides)

    return ExportResult(
        out_dir=out_dir,
        lecture=lecture,
        transcript_count=len(segments),
        slide_count=len(slides),
        image_count=image_count,
        new_image_count=new_image_count,
        lecture_md=lecture_md,
        notes=notes,
    )
