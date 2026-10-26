"""智云课堂 API 客户端。

已确认的接口：
  - 我的课程：education.cmc.zju.edu.cn/.../account-profile/course
  - 课时目录：yjapi.cmc.zju.edu.cn/courseapi/v2/course/catalogue
  - 字幕：yjapi.cmc.zju.edu.cn/courseapi/v3/web-socket/search-trans-result
  - 课件时间轴：classroom.zju.edu.cn/pptnote/v1/schedule/search-ppt
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests

from .auth import jwt_payload
from .errors import ApiError, AuthExpiredError
from .net import get_ci, is_zju_url, make_session, parse_json_maybe, to_int


URL_MY_COURSES = (
    "https://education.cmc.zju.edu.cn/personal/courseapi/"
    "vlabpassportapi/v1/account-profile/course"
)
URL_CATALOGUE = "https://yjapi.cmc.zju.edu.cn/courseapi/v2/course/catalogue"
URL_DETAIL = "https://yjapi.cmc.zju.edu.cn/courseapi/v3/multi-search/get-course-detail"
URL_TRANSCRIPT = (
    "https://yjapi.cmc.zju.edu.cn/courseapi/v3/web-socket/search-trans-result"
)
URL_SLIDES = "https://classroom.zju.edu.cn/pptnote/v1/schedule/search-ppt"
CLASSROOM_HOME = "https://classroom.zju.edu.cn/"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/x-ms-bmp": ".bmp",
}

_thread_local = threading.local()


def _thread_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = make_session()
        _thread_local.session = session
    return session


def _clean_text(value: Any) -> str:
    text = str(value or "").replace("​", "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # 中文 ASR 常在汉字间插入空格，去掉；保留中文与英文/数字之间的空格。
    text = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", text)
    return text


def _normalize_image_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return urljoin(CLASSROOM_HOME, url)
    if not url.lower().startswith(("http://", "https://")):
        return urljoin(CLASSROOM_HOME, url)
    return url


class ZhiyunApi:
    def __init__(
        self,
        jwt: str,
        *,
        student_id: str = "",
        username: str = "",
        timeout: float = 30.0,
    ):
        self.jwt = str(jwt or "").strip()
        if self.jwt.lower().startswith("bearer "):
            self.jwt = self.jwt[7:].strip()
        if not self.jwt:
            raise AuthExpiredError("缺少智云课堂凭证，请先运行 `zhiyun login`。")

        self.student_id = student_id
        self.username = username
        self.timeout = timeout
        self.payload = jwt_payload(self.jwt)
        self.user_id = str(self.payload.get("sub") or self.payload.get("uid") or "")
        self.last_raw_transcript: Any = None
        self.last_raw_slides: list[dict[str, Any]] = []
        self.last_raw_lectures: Any = None

        self.session = make_session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.jwt}",
                "Accept": "application/json, text/plain, */*",
            }
        )
        # 部分 classroom 接口会读 cookie，补一个同名 cookie，不影响 yjapi 的 Bearer 鉴权。
        try:
            self.session.cookies.set(
                "_token", self.jwt, domain="classroom.zju.edu.cn", path="/"
            )
        except Exception:  # pragma: no cover - 兼容不同 requests 版本
            pass

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        referer: str = "",
    ) -> Any:
        request_headers = dict(headers or {})
        if referer:
            request_headers.setdefault("Referer", referer)
        try:
            resp = self.session.request(
                method,
                url,
                params=params,
                headers=request_headers,
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise ApiError(f"请求失败：{exc}") from exc

        if resp.status_code in (401, 403):
            snippet = re.sub(r"\s+", " ", resp.text or "")[:160].strip()
            raise AuthExpiredError(
                f"智云接口返回 HTTP {resp.status_code}"
                + (f"（{snippet}）" if snippet else "")
                + "。登录可能已过期，请重新运行 `zhiyun login`。"
            )
        if resp.status_code >= 400:
            snippet = re.sub(r"\s+", " ", resp.text or "")[:160].strip()
            raise ApiError(f"接口 {url} 返回 HTTP {resp.status_code}" + (f"：{snippet}" if snippet else ""))

        content_type = (resp.headers.get("content-type") or "").lower()
        if "json" not in content_type and not (resp.text or "").lstrip().startswith(("{", "[")):
            raise ApiError(
                f"接口 {url} 没有返回 JSON（Content-Type: {content_type or '未知'}），"
                "可能需要重新登录或平台接口已变化。"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError(f"接口 {url} 返回了无法解析的 JSON") from exc

    # ------------------------------------------------------------------
    # 课程
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_course_items(data: Any) -> list[dict[str, Any]]:
        candidates: list[list[dict[str, Any]]] = []
        roots: list[Any] = []
        if isinstance(data, dict):
            roots.append(data)
            params = data.get("params")
            if isinstance(params, dict):
                roots.append(params.get("result"))
            roots.append(data.get("result"))
            roots.append(data.get("data"))
        for root in roots:
            if not isinstance(root, dict):
                continue
            for key in ("data", "models", "list", "course_list"):
                value = root.get(key)
                if isinstance(value, list):
                    candidates.append([x for x in value if isinstance(x, dict)])
                elif isinstance(value, dict):
                    for nested_key in ("data", "models", "list"):
                        nested = value.get(nested_key)
                        if isinstance(nested, list):
                            candidates.append([x for x in nested if isinstance(x, dict)])
        if not candidates:
            return []
        return max(candidates, key=len)

    @staticmethod
    def _normalize_course(raw: dict[str, Any]) -> dict[str, Any]:
        course_id = get_ci(raw, "Id", "id", "course_id", "CourseId")
        if not course_id:
            return {}
        info = parse_json_maybe(get_ci(raw, "information", "Information"))

        teachers_raw = get_ci(raw, "Teachers", "teachers", default=[])
        teacher_names: list[str] = []
        if isinstance(teachers_raw, list):
            for item in teachers_raw:
                if isinstance(item, dict):
                    name = get_ci(item, "Realname", "realname", "name")
                    if name:
                        teacher_names.append(str(name))
                elif item:
                    teacher_names.append(str(item))
        direct_teacher = get_ci(raw, "Teacher", "teacher", "realname", "teacher_name")
        if direct_teacher:
            teacher_names.append(str(direct_teacher))

        teacher = "、".join(dict.fromkeys(name for name in teacher_names if name))
        title = str(get_ci(raw, "Title", "title", "course_name", "name") or f"课程 {course_id}")
        term = str(get_ci(raw, "TermName", "term_name", "term", "semester") or "")
        college = str(get_ci(raw, "KkxyName", "kkxy_name", "college", "department") or "")

        return {
            "course_id": str(course_id),
            "title": title,
            "term": term,
            "teacher": teacher,
            "college": college,
            "course_code": str(
                info.get("kcdm")
                or info.get("course_code")
                or get_ci(raw, "CourseCode", "course_code")
                or ""
            ),
            "prev_sub_id": str(get_ci(raw, "PrevSubjectId", "prev_sub_id", default="") or ""),
        }

    def my_courses(self) -> list[dict[str, Any]]:
        """当前账号的课程列表。优先 force_mycourse=1，空则退回 0。"""
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }
        referer = "https://education.cmc.zju.edu.cn/personal/"

        last_items: list[dict[str, Any]] = []
        for force in (1, 0):
            courses: dict[str, dict[str, Any]] = {}
            page = 1
            while page <= 30:
                data = self._request(
                    "GET",
                    URL_MY_COURSES,
                    params={
                        "nowpage": page,
                        "per-page": 100,
                        "force_mycourse": force,
                    },
                    headers=headers,
                    referer=referer,
                )
                raw_items = self._extract_course_items(data)
                if not raw_items:
                    break
                new_count = 0
                for raw in raw_items:
                    course = self._normalize_course(raw)
                    if course and course["course_id"] not in courses:
                        courses[course["course_id"]] = course
                        new_count += 1
                last_items = list(courses.values())
                if len(raw_items) < 100 or new_count == 0:
                    break
                page += 1
            if courses:
                return list(courses.values())
        return last_items

    # ------------------------------------------------------------------
    # 课时
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_lecture(raw: dict[str, Any], course_id: str) -> dict[str, Any]:
        sub_id = get_ci(raw, "sub_id", "subject_id", "id", "subId")
        if not sub_id:
            return {}
        content = parse_json_maybe(get_ci(raw, "content", "Content"))
        playback = content.get("playback") if isinstance(content, dict) else {}
        playback_url = ""
        if isinstance(playback, dict):
            playback_url = str(playback.get("url") or playback.get("playback_url") or "")

        status = str(get_ci(raw, "status", "sub_status", "Status", "SubStatus", default="") or "")
        title = str(
            get_ci(raw, "title", "sub_title", "name", "Title", "SubTitle")
            or f"课时 {sub_id}"
        )
        start_at = get_ci(raw, "start_at", "startAt", "begin_at", "start_time", default="")
        end_at = get_ci(raw, "end_at", "endAt", "end_time", default="")
        duration = to_int(get_ci(raw, "duration", "length", "video_duration"), 0)
        lecturer = str(get_ci(raw, "lecturer_name", "lecturerName", "realname", "teacher") or "")

        return {
            "course_id": str(
                get_ci(raw, "course_id", "courseId", default=course_id) or course_id
            ),
            "sub_id": str(sub_id),
            "title": title,
            "status": status,
            "start_at": start_at,
            "end_at": end_at,
            "duration": duration,
            "lecturer": lecturer,
            "playback_url": playback_url,
            "has_transcript": status == "6",
        }

    @staticmethod
    def _extract_lecture_dicts(obj: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        def looks_like_lecture(item: dict[str, Any]) -> bool:
            has_id = any(key in item for key in ("sub_id", "subject_id"))
            if not has_id:
                # 详情接口使用 id + sub_title
                has_id = "id" in item and any(
                    key in item for key in ("sub_title", "sub_status", "start_at")
                )
            has_title = any(
                key in item for key in ("title", "sub_title", "start_at", "sub_status")
            )
            return has_id and has_title

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if looks_like_lecture(value):
                    found.append(value)
                else:
                    for child in value.values():
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(obj)
        return found

    def _lectures_via_detail(self, course_id: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"course_id": str(course_id)}
        if self.student_id:
            params["student"] = self.student_id
        data = self._request(
            "GET",
            URL_DETAIL,
            params=params,
            referer=f"https://classroom.zju.edu.cn/coursedetail?course_id={course_id}&tenant_code=112",
        )
        raw_items = self._extract_lecture_dicts(data)
        result: list[dict[str, Any]] = []
        for raw in raw_items:
            if "sub_id" not in raw and "subject_id" not in raw and "id" in raw:
                raw = dict(raw)
                raw["sub_id"] = raw["id"]
            if "title" not in raw and "sub_title" in raw:
                raw = dict(raw)
                raw["title"] = raw["sub_title"]
            if "status" not in raw and "sub_status" in raw:
                raw = dict(raw)
                raw["status"] = raw["sub_status"]
            lecture = self._normalize_lecture(raw, course_id)
            if lecture:
                result.append(lecture)
        return result

    def lectures(self, course_id: str) -> list[dict[str, Any]]:
        """课时目录，按时间从早到晚，index 从 1 开始。"""
        data = self._request(
            "GET",
            URL_CATALOGUE,
            params={"course_id": str(course_id)},
            referer=f"https://classroom.zju.edu.cn/coursedetail?course_id={course_id}&tenant_code=112",
        )
        self.last_raw_lectures = data
        raw_items = self._extract_lecture_dicts(data)

        lectures: dict[str, dict[str, Any]] = {}
        for raw in raw_items:
            lecture = self._normalize_lecture(raw, course_id)
            if lecture and lecture["sub_id"] not in lectures:
                lectures[lecture["sub_id"]] = lecture

        if not lectures:
            for raw in self._lectures_via_detail(course_id):
                lectures.setdefault(raw["sub_id"], raw)
        if not lectures:
            raise ApiError(
                "没能在这个课程下找到课时。请确认课程 ID 是否正确、账号是否有权限，"
                "以及该课程是否使用智云课堂录制。"
            )

        ordered = sorted(
            lectures.values(),
            key=lambda item: (to_int(item.get("start_at"), 0), to_int(item.get("sub_id"), 0)),
        )
        for index, lecture in enumerate(ordered, 1):
            lecture["index"] = index
        return ordered

    # ------------------------------------------------------------------
    # 字幕
    # ------------------------------------------------------------------

    @staticmethod
    def _segment_candidates(payload: Any) -> list[list[Any]]:
        candidates: list[list[Any]] = []

        def add(value: Any) -> None:
            if isinstance(value, list):
                candidates.append(value)

        if isinstance(payload, list):
            add(payload)
        if isinstance(payload, dict):
            for key in ("list", "segments", "data", "result", "transcript"):
                value = payload.get(key)
                add(value)
                if isinstance(value, dict):
                    for nested_key in ("list", "segments", "data", "all_content"):
                        add(value.get(nested_key))
            add(payload.get("all_content"))
        if isinstance(payload, dict) and isinstance(payload.get("all_content"), list):
            candidates.append(payload["all_content"])
        return candidates

    @classmethod
    def _extract_segments(cls, payload: Any) -> list[dict[str, Any]]:
        best: list[Any] = []
        best_score = -1
        for candidate in cls._segment_candidates(payload):
            score = 0
            for item in candidate:
                if isinstance(item, dict):
                    if isinstance(item.get("all_content"), list):
                        score += 2
                    elif any(
                        key in item
                        for key in (
                            "text", "Text", "BeginSec", "start_time", "startTime",
                            "content", "Content", "start_sec",
                        )
                    ):
                        score += 1
            if score > best_score:
                best = candidate
                best_score = score

        segments: list[Any] = []
        for item in best:
            if isinstance(item, dict) and isinstance(item.get("all_content"), list):
                segments.extend(item["all_content"])
            elif isinstance(item, dict) and isinstance(item.get("content"), list):
                segments.extend(item["content"])
            else:
                segments.append(item)
        return [item for item in segments if isinstance(item, dict)]

    @classmethod
    def _normalize_transcript(cls, payload: Any) -> list[dict[str, Any]]:
        segments = cls._extract_segments(payload)
        normalized: list[dict[str, Any]] = []
        seen_text = ""
        for seg in segments:
            text_value = get_ci(
                seg,
                "text", "Text", "content_text", "word", "transcript", "sentence",
            )
            if not text_value:
                maybe_text = get_ci(seg, "content", "Content")
                if isinstance(maybe_text, str):
                    text_value = maybe_text
            text = _clean_text(text_value)
            if not text or text == seen_text:
                continue

            # 秒字段：BeginSec / start_sec 等；毫秒字段：start_time / startTime。
            # 起止时间可能来自不同命名的字段，分开判断单位，避免把秒当毫秒。
            lower_seg = {str(key).lower(): value for key, value in seg.items()}

            def first_present(keys: tuple[str, ...]) -> tuple[bool, Any]:
                for key in keys:
                    lowered = key.lower()
                    if lowered in lower_seg:
                        return True, lower_seg[lowered]
                return False, None

            start_is_sec, start_raw = first_present(
                (
                    "BeginSec", "begin_sec", "begin", "start_sec", "startSec",
                    "start_second", "start_seconds",
                )
            )
            start_is_ms = False
            if not start_is_sec:
                start_is_ms, start_raw = first_present(
                    (
                        "start_time", "startTime", "begin_time", "beginTime",
                        "start_ms", "startMs",
                    )
                )

            end_is_sec, end_raw = first_present(
                (
                    "EndSec", "end_sec", "endSec", "end", "end_second", "end_seconds",
                )
            )
            end_is_ms = False
            if not end_is_sec:
                end_is_ms, end_raw = first_present(
                    (
                        "end_time", "endTime", "end_ms", "endMs",
                    )
                )

            try:
                start_num = float(start_raw or 0)
                end_num = float(end_raw or start_raw or 0)
            except (TypeError, ValueError):
                start_num = end_num = 0.0

            if start_is_sec:
                start_sec = int(start_num)
            elif start_is_ms:
                start_sec = int(start_num / 1000)
            else:
                start_sec = 0

            if end_is_sec:
                end_sec = int(end_num)
            elif end_is_ms:
                end_sec = int(end_num / 1000)
            else:
                end_sec = start_sec

            start_sec = max(0, start_sec)
            end_sec = max(start_sec, end_sec)
            normalized.append(
                {
                    "start": start_sec,
                    "end": end_sec,
                    "text": text,
                }
            )
            seen_text = text

        normalized.sort(key=lambda item: item["start"])
        return normalized

    def transcript(self, sub_id: str) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            URL_TRANSCRIPT,
            params={"sub_id": str(sub_id), "format": "json"},
            referer=f"https://classroom.zju.edu.cn/livingroom?sub_id={sub_id}",
        )
        self.last_raw_transcript = data
        if isinstance(data, dict):
            code = data.get("code")
            result = data.get("result")
            if isinstance(result, dict) and result.get("code") in (401, 403):
                raise AuthExpiredError("字幕接口鉴权失败，请重新运行 `zhiyun login`。")
            if code in (401, 403):
                raise AuthExpiredError("字幕接口鉴权失败，请重新运行 `zhiyun login`。")
        return self._normalize_transcript(data)

    # ------------------------------------------------------------------
    # 课件
    # ------------------------------------------------------------------

    def slide_timeline(self, course_id: str, sub_id: str) -> list[dict[str, Any]]:
        headers = {"X-Requested-With": "XMLHttpRequest"}
        referer = f"https://classroom.zju.edu.cn/livingroom?sub_id={sub_id}"
        slides: dict[tuple[str, int, str], dict[str, Any]] = {}
        self.last_raw_slides = []

        for page in range(1, 21):
            data = self._request(
                "GET",
                URL_SLIDES,
                params={
                    "course_id": str(course_id),
                    "sub_id": str(sub_id),
                    "page": page,
                    "per_page": 100,
                },
                headers=headers,
                referer=referer,
            )
            self.last_raw_slides.append({"page": page, "data": data})
            if isinstance(data, dict):
                raw_list = data.get("list")
                if not isinstance(raw_list, list):
                    nested = data.get("data")
                    raw_list = nested.get("list") if isinstance(nested, dict) else None
            else:
                raw_list = None
            if not raw_list:
                break

            new_count = 0
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                content = parse_json_maybe(get_ci(item, "content", "Content"))
                if not isinstance(content, dict):
                    continue
                url = _normalize_image_url(
                    str(
                        content.get("pptimgurl")
                        or content.get("pptImgUrl")
                        or content.get("imgurl")
                        or content.get("url")
                        or ""
                    )
                )
                if not url:
                    continue
                created_sec = to_int(
                    get_ci(item, "created_sec", "createdSec", "time", "created_at"), 0
                )
                slide_id = str(get_ci(item, "id", "slide_id", "slideId", default="") or "")
                key = (slide_id, created_sec, url)
                if key in slides:
                    continue
                slides[key] = {
                    "slide_id": slide_id,
                    "time": created_sec,
                    "url": url,
                    "title": str(content.get("title") or content.get("name") or ""),
                    "raw": item,
                }
                new_count += 1

            if len(raw_list) < 100 or new_count == 0:
                break

        ordered = sorted(
            slides.values(),
            key=lambda item: (item["time"], to_int(item["slide_id"], 0), item["url"]),
        )
        # 连续截图完全相同（同一页 PPT 停留时的重复帧）只保留第一张。
        deduped: list[dict[str, Any]] = []
        for slide in ordered:
            if deduped and deduped[-1]["url"] == slide["url"]:
                continue
            deduped.append(slide)
        for index, slide in enumerate(deduped, 1):
            slide["index"] = index
        return deduped

    # ------------------------------------------------------------------
    # 图片下载
    # ------------------------------------------------------------------

    def download_image(
        self,
        url: str,
        dest_stem: Path,
        *,
        referer: str = "",
        force: bool = False,
    ) -> tuple[Path | None, bool]:
        """下载课件图片，自动按 Content-Type 决定扩展名。

        返回 (路径, 是否本次新下载)。已存在且非 --force 时返回 (旧文件, False)。
        """
        url = _normalize_image_url(url)
        if not url:
            return None, False

        for ext in IMAGE_EXTS:
            existing = Path(str(dest_stem) + ext)
            if existing.exists() and existing.stat().st_size > 0 and not force:
                return existing, False

        headers = {
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": referer or CLASSROOM_HOME,
        }
        if is_zju_url(url):
            headers["Authorization"] = f"Bearer {self.jwt}"

        session = _thread_session()
        if is_zju_url(url):
            host = urlparse(url).hostname or ""
            try:
                session.cookies.set("_token", self.jwt, domain=host, path="/")
            except Exception:
                pass

        part_path = Path(str(dest_stem) + ".part")
        try:
            with session.get(url, headers=headers, timeout=(10, 60), stream=True) as resp:
                if resp.status_code in (401, 403):
                    raise AuthExpiredError(
                        f"课件图片返回 HTTP {resp.status_code}，登录可能已过期；URL: {url}"
                    )
                if resp.status_code >= 400:
                    raise ApiError(f"下载课件图片失败：HTTP {resp.status_code}；URL: {url}")

                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if "text/html" in content_type:
                    raise ApiError(f"课件图片返回了 HTML（可能需要登录）；URL: {url}")

                ext = CONTENT_TYPE_EXT.get(content_type)
                if not ext:
                    suffix = Path(urlparse(url).path).suffix.lower()
                    ext = suffix if suffix in IMAGE_EXTS else ""

                part_path.parent.mkdir(parents=True, exist_ok=True)
                first_chunk = b""
                total = 0
                with open(part_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        if not first_chunk:
                            first_chunk = chunk[:16]
                        total += len(chunk)
                        fh.write(chunk)

                if not ext:
                    if first_chunk.startswith(b"\xff\xd8\xff"):
                        ext = ".jpg"
                    elif first_chunk.startswith(b"\x89PNG"):
                        ext = ".png"
                    elif first_chunk[:6] in (b"GIF87a", b"GIF89a"):
                        ext = ".gif"
                    elif first_chunk[:4] == b"RIFF" and first_chunk[8:12] == b"WEBP":
                        ext = ".webp"
                    elif first_chunk.startswith(b"BM"):
                        ext = ".bmp"
                    else:
                        ext = ".jpg"

                if total <= 0:
                    raise ApiError(f"课件图片为空；URL: {url}")

                final_path = Path(str(dest_stem) + ext)
                part_path.replace(final_path)
                return final_path, True
        except Exception:
            try:
                part_path.unlink()
            except OSError:
                pass
            raise
