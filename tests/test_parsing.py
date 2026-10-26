from __future__ import annotations

import base64
import json
import unittest
from datetime import datetime

from zhiyun.api import ZhiyunApi
from zhiyun.auth import (
    encrypt_password,
    extract_jwt_from_pairs,
    jwt_payload,
    normalize_jwt,
)
from zhiyun.export import fmt_clock, make_paragraphs, parse_datetime, safe_name


def _b64(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_jwt(payload: dict) -> str:
    return _b64({"alg": "none", "typ": "JWT"}) + "." + _b64(payload) + ".abcdefgh"


class JwtTests(unittest.TestCase):
    def test_payload_and_normalize(self):
        token = make_jwt({"sub": "3230000000", "exp": 1700000000})
        self.assertEqual(jwt_payload(token)["sub"], "3230000000")
        self.assertEqual(normalize_jwt(f"Bearer {token}"), token)
        self.assertEqual(normalize_jwt(f'cookie="{token}"'), token)

    def test_extract_from_cookie_pairs(self):
        token = make_jwt({"sub": "1", "exp": 1700000000})
        pairs = [("PHPSESSID", "abc"), ("_token", token)]
        self.assertEqual(extract_jwt_from_pairs(pairs), token)

    def test_extract_from_url_encoded_cookie(self):
        token = make_jwt({"sub": "2", "exp": 1700000000})
        encoded = "%22" + token + "%22"
        self.assertEqual(extract_jwt_from_pairs([("_identity", encoded)]), token)

    def test_bad_token(self):
        self.assertEqual(jwt_payload("not-a-jwt"), {})
        self.assertFalse(normalize_jwt("hello"))


class RsaTests(unittest.TestCase):
    def test_matches_official_frontend(self):
        # 2026-10 抓取的登录公钥；该测试锁定“裸 RSA + 大端编码”的实现。
        modulus = (
            "c510865608e3dffc7ea194ddfe6db3139dc19a02d8bb2391ef83be56ac5e1bc5"
            "d05c4b49faf00102c477ca62d40710f77966cdf57b9103c33352aafda3e00083"
        )
        expected = (
            "9362057723f6c723eac9d2de741fcfc69b7cac50ccdfbe4d58732e56176f2c18"
            "b0221025542b9108db5d71e2b038e07e98481f7acbf0dfda8a45de8aefcc3e68"
        )
        self.assertEqual(encrypt_password("TestPassword123!", modulus, "10001"), expected)


class TranscriptTests(unittest.TestCase):
    def test_all_content_seconds(self):
        payload = {
            "list": [
                {
                    "all_content": [
                        {"BeginSec": 0, "EndSec": 2, "Text": "大家好"},
                        {"BeginSec": 2, "EndSec": 5, "Text": "今天讲排序"},
                    ]
                }
            ]
        }
        segments = ZhiyunApi._normalize_transcript(payload)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["start"], 0)
        self.assertEqual(segments[1]["start"], 2)
        self.assertEqual(segments[1]["text"], "今天讲排序")

    def test_millisecond_fields(self):
        payload = {"list": [{"start_time": 125000, "end_time": 128000, "text": "hello"}]}
        segments = ZhiyunApi._normalize_transcript(payload)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["start"], 125)
        self.assertEqual(segments[0]["end"], 128)

    def test_nested_data_and_dedup(self):
        payload = {
            "data": {
                "list": [
                    {"BeginSec": 10, "EndSec": 12, "Text": "你好"},
                    {"BeginSec": 12, "EndSec": 14, "Text": "你好"},
                ]
            }
        }
        segments = ZhiyunApi._normalize_transcript(payload)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["start"], 10)

    def test_empty(self):
        self.assertEqual(ZhiyunApi._normalize_transcript({"list": []}), [])
        self.assertEqual(ZhiyunApi._normalize_transcript(None), [])


class LectureExtractionTests(unittest.TestCase):
    def test_flat_catalogue(self):
        payload = {
            "result": {
                "data": [
                    {"sub_id": 2, "title": "第2讲", "status": "6", "start_at": "200"},
                    {"sub_id": 1, "title": "第1讲", "status": "3", "start_at": "100"},
                ]
            }
        }
        items = ZhiyunApi._extract_lecture_dicts(payload)
        self.assertEqual(len(items), 2)
        first = ZhiyunApi._normalize_lecture(items[1], "9")
        self.assertEqual(first["sub_id"], "1")
        self.assertFalse(first["has_transcript"])

    def test_nested_detail(self):
        payload = {
            "data": {
                "sub_list": {
                    "chapter": [
                        {"id": 55, "sub_title": "第1讲", "sub_status": 6},
                    ]
                }
            }
        }
        items = ZhiyunApi._extract_lecture_dicts(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], 55)


class TextTests(unittest.TestCase):
    def test_paragraphs(self):
        segments = [
            {"start": 0, "end": 2, "text": "大家好"},
            {"start": 2, "end": 4, "text": "今天讲排序"},
            {"start": 20, "end": 22, "text": "先看冒泡排序"},
        ]
        paragraphs = make_paragraphs(segments, gap_seconds=6)
        self.assertEqual(len(paragraphs), 2)
        self.assertEqual(paragraphs[0]["text"], "大家好今天讲排序")
        self.assertEqual(paragraphs[1]["text"], "先看冒泡排序")

    def test_paragraphs_break_by_span(self):
        segments = [
            {"start": index, "end": index + 1, "text": f"第{index}句话。"}
            for index in range(60)
        ]
        paragraphs = make_paragraphs(segments, gap_seconds=6, max_seconds=10, max_chars=1000)
        self.assertGreater(len(paragraphs), 3)
        self.assertTrue(all(paragraph["text"].endswith("。") for paragraph in paragraphs))
        self.assertLessEqual(max(paragraph["end"] - paragraph["start"] for paragraph in paragraphs), 11)

    def test_clock(self):
        self.assertEqual(fmt_clock(0), "00:00:00")
        self.assertEqual(fmt_clock(3725), "01:02:05")

    def test_safe_name(self):
        name = safe_name('a/b:c*?"<>|  d')
        for char in '\\/:*?"<>|':
            self.assertNotIn(char, name)
        self.assertTrue(name)
        self.assertEqual(safe_name(""), "untitled")

    def test_parse_datetime(self):
        self.assertEqual(
            parse_datetime("1700000000000"),
            datetime.fromtimestamp(1700000000),
        )
        self.assertEqual(
            parse_datetime("1700000000"),
            datetime.fromtimestamp(1700000000),
        )
        self.assertIsNone(parse_datetime("not-a-time"))


if __name__ == "__main__":
    unittest.main()
