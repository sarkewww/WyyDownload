"""测试工具函数"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from main import safe_filename, ILLEGAL_CHARS


class TestSafeFilename(unittest.TestCase):
    def test_normal_name(self):
        result = safe_filename("Hello World")
        self.assertEqual(result, "Hello World")

    def test_illegal_chars(self):
        result = safe_filename('<>:"/\\|?*test')
        self.assertEqual(result, "test")

    def test_empty_name(self):
        result = safe_filename("")
        self.assertEqual(result, "file")

    def test_none_name(self):
        result = safe_filename(None)
        self.assertEqual(result, "file")

    def test_chinese_name(self):
        result = safe_filename("万能青年旅店 - 不万能的喜剧")
        self.assertEqual(result, "万能青年旅店 - 不万能的喜剧")


class TestFormatFunctions(unittest.TestCase):
    def test_format_speed(self):
        from main import _format_speed
        self.assertEqual(_format_speed(0), "0 B/s")
        self.assertEqual(_format_speed(1024), "1.0 KB/s")
        self.assertEqual(_format_speed(1048576), "1.0 MB/s")

    def test_format_eta(self):
        from main import _format_eta
        self.assertEqual(_format_eta(-1), "--")
        self.assertEqual(_format_eta(0), "0秒")
        self.assertEqual(_format_eta(65), "1分5秒")
        self.assertEqual(_format_eta(3661), "1时1分")


if __name__ == "__main__":
    unittest.main()
