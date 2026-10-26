from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zhiyun.envfile import load_env_file, parse_env


class EnvParseTests(unittest.TestCase):
    def test_one_line_semicolon_format(self):
        parsed = parse_env("A = 3230000000; P = s3cret!;\n")
        self.assertEqual(parsed["ZHIYUN_USERNAME"], "3230000000")
        self.assertEqual(parsed["ZHIYUN_PASSWORD"], "s3cret!")

    def test_standard_lines_and_comments(self):
        parsed = parse_env(
            """
            # 学号
            ZHIYUN_USERNAME=3230000000
            ZHIYUN_PASSWORD=p@ss word  # 行内注释
            OTHER=ignored
            """
        )
        self.assertEqual(parsed["ZHIYUN_USERNAME"], "3230000000")
        self.assertEqual(parsed["ZHIYUN_PASSWORD"], "p@ss word")
        self.assertNotIn("OTHER", parsed)

    def test_export_prefix(self):
        parsed = parse_env("export A=323; export P=pw;\n")
        self.assertEqual(parsed["ZHIYUN_USERNAME"], "323")
        self.assertEqual(parsed["ZHIYUN_PASSWORD"], "pw")

    def test_quoted_semicolon_in_password(self):
        parsed = parse_env('A = 323; P = "a;b;c";\n')
        self.assertEqual(parsed["ZHIYUN_PASSWORD"], "a;b;c")

    def test_plain_password_with_semicolon(self):
        parsed = parse_env("ZHIYUN_USERNAME=323\nZHIYUN_PASSWORD=abc;def\n")
        self.assertEqual(parsed["ZHIYUN_PASSWORD"], "abc;def")

    def test_hash_in_password(self):
        parsed = parse_env("A=323\nP=abc#def\n")
        self.assertEqual(parsed["ZHIYUN_PASSWORD"], "abc#def")

    def test_canonical_name_wins_over_alias(self):
        parsed = parse_env("A=alias\nZHIYUN_USERNAME=canonical\n")
        self.assertEqual(parsed["ZHIYUN_USERNAME"], "canonical")

    def test_aliases(self):
        parsed = parse_env("ZJU_USERNAME=323\nZJU_PASSWORD=pw\nTOKEN=abc\n")
        self.assertEqual(parsed["ZHIYUN_USERNAME"], "323")
        self.assertEqual(parsed["ZHIYUN_PASSWORD"], "pw")
        self.assertEqual(parsed["ZHIYUN_TOKEN"], "abc")

    def test_empty_values_ignored(self):
        parsed = parse_env("A=\nP=\n")
        self.assertEqual(parsed, {})


class EnvLoadTests(unittest.TestCase):
    def test_real_environment_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("A=from-file\nP=pw\n", encoding="utf-8")
            path.chmod(0o600)
            with mock.patch.dict(os.environ, {"ZHIYUN_USERNAME": "existing"}, clear=True):
                parsed = load_env_file(path)
                self.assertEqual(os.environ["ZHIYUN_USERNAME"], "existing")
                self.assertEqual(os.environ["ZHIYUN_PASSWORD"], "pw")
                self.assertEqual(parsed["ZHIYUN_USERNAME"], "from-file")

    def test_missing_explicit_file(self):
        with self.assertRaises(Exception):
            load_env_file(Path("/nonexistent/definitely-no.env"), required=True)


if __name__ == "__main__":
    unittest.main()
