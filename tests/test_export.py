from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zhiyun.api import ZhiyunApi
from zhiyun.export import ExportOptions, export_lecture


class FakeApi:
    def __init__(self):
        self.last_raw_transcript = {"list": [{"all_content": []}]}
        self.last_raw_slides = [{"page": 1, "data": {"list": []}}]

    def transcript(self, sub_id):
        return [
            {"start": 0, "end": 4, "text": "大家好"},
            {"start": 4, "end": 8, "text": "今天讲排序"},
            {"start": 30, "end": 34, "text": "先看冒泡排序"},
        ]

    def slide_timeline(self, course_id, sub_id):
        return [
            {
                "index": 1,
                "slide_id": "101",
                "time": 0,
                "url": "https://example.invalid/1.jpg",
                "title": "封面",
            },
            {
                "index": 2,
                "slide_id": "102",
                "time": 20,
                "url": "https://example.invalid/2.jpg",
                "title": "排序算法",
            },
        ]

    def download_image(self, url, dest_stem, *, referer="", force=False):
        path = Path(str(dest_stem) + ".jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
        return path, True


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / "output"
        self.course = {
            "course_id": "9",
            "title": "数据结构",
            "teacher": "张三",
            "term": "2024-2025 秋冬",
            "college": "计算机学院",
            "course_code": "CS101",
        }
        self.lecture = {
            "index": 3,
            "sub_id": "55",
            "title": "第3讲 排序",
            "status": "6",
            "start_at": "1700000000",
            "end_at": "1700003600",
            "has_transcript": True,
            "playback_url": "https://example.invalid/play.m3u8",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_export_all_artifacts(self):
        result = export_lecture(
            FakeApi(),
            self.course,
            self.lecture,
            ExportOptions(base_dir=self.base, save_raw=True),
            log=lambda message: None,
        )
        out = result.out_dir
        self.assertTrue((out / "transcript.md").exists())
        self.assertTrue((out / "transcript.txt").exists())
        self.assertTrue((out / "transcript.json").exists())
        self.assertTrue((out / "slides.json").exists())
        self.assertTrue((out / "metadata.json").exists())
        self.assertTrue((out / "raw" / "transcript.json").exists())
        self.assertEqual(result.transcript_count, 3)
        self.assertEqual(result.slide_count, 2)
        self.assertEqual(result.image_count, 2)
        self.assertEqual(result.new_image_count, 2)

        slides = sorted((out / "slides").iterdir())
        self.assertEqual(len(slides), 2)
        self.assertEqual(slides[0].name, "001_at_00-00-00.jpg")

        lecture_md = (out / "lecture.md").read_text(encoding="utf-8")
        self.assertIn("### [00:00:00] 课件 1", lecture_md)
        self.assertIn("- [00:00:00] 大家好", lecture_md)
        self.assertIn("![课件 1](slides/001_at_00-00-00.jpg)", lecture_md)

        transcript_md = (out / "transcript.md").read_text(encoding="utf-8")
        self.assertIn("**[00:00:00]** 大家好今天讲排序", transcript_md)

        plain = (out / "transcript.txt").read_text(encoding="utf-8")
        self.assertIn("大家好今天讲排序", plain)
        self.assertIn("先看冒泡排序", plain)

        metadata = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["lecture"]["sub_id"], "55")
        self.assertEqual(metadata["counts"]["images_available"], 2)
        self.assertEqual(metadata["counts"]["images_downloaded_now"], 2)
        self.assertEqual(metadata["course"]["title"], "数据结构")

    def test_no_images(self):
        result = export_lecture(
            FakeApi(),
            self.course,
            self.lecture,
            ExportOptions(base_dir=self.base, images=False),
            log=lambda message: None,
        )
        self.assertEqual(result.image_count, 0)
        self.assertEqual(result.new_image_count, 0)
        self.assertFalse((result.out_dir / "slides").exists())
        self.assertIn("课件图片未下载", (result.out_dir / "lecture.md").read_text(encoding="utf-8"))

    def test_flat_collision(self):
        export_lecture(
            FakeApi(),
            self.course,
            self.lecture,
            ExportOptions(base_dir=self.base / "flat", flat=True),
            log=lambda message: None,
        )
        other = dict(self.lecture, sub_id="56")
        with self.assertRaises(Exception):
            export_lecture(
                FakeApi(),
                self.course,
                other,
                ExportOptions(base_dir=self.base / "flat", flat=True),
                log=lambda message: None,
            )


class CourseParsingTests(unittest.TestCase):
    def test_extract_and_normalize(self):
        payload = {
            "params": {
                "result": {
                    "data": [
                        {
                            "Id": 123,
                            "Title": "数据结构",
                            "TermName": "2024-2025 秋冬",
                            "Teachers": [{"Realname": "张三"}, {"Realname": "李四"}],
                            "information": '{"kcdm": "CS101"}',
                        }
                    ]
                }
            }
        }
        items = ZhiyunApi._extract_course_items(payload)
        self.assertEqual(len(items), 1)
        course = ZhiyunApi._normalize_course(items[0])
        self.assertEqual(course["course_id"], "123")
        self.assertEqual(course["title"], "数据结构")
        self.assertEqual(course["teacher"], "张三、李四")
        self.assertEqual(course["course_code"], "CS101")


if __name__ == "__main__":
    unittest.main()
