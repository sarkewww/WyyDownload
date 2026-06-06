"""测试下载器模块"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from music_downloader import MusicDownloader


class TestFileExtension(unittest.TestCase):
    def setUp(self):
        self.dl = MusicDownloader()

    def test_flac_from_url(self):
        ext = self.dl._determine_file_extension(
            "https://example.com/song.flac?param=xxx")
        self.assertEqual(ext, ".flac")

    def test_mp3_from_url(self):
        ext = self.dl._determine_file_extension(
            "https://example.com/song.mp3")
        self.assertEqual(ext, ".mp3")

    def test_m4a_from_url(self):
        ext = self.dl._determine_file_extension(
            "https://example.com/song.m4a")
        self.assertEqual(ext, ".m4a")

    def test_from_content_type_flac(self):
        ext = self.dl._determine_file_extension(
            "https://example.com/stream", content_type="flac")
        self.assertEqual(ext, ".flac")

    def test_from_content_type_mp3(self):
        ext = self.dl._determine_file_extension(
            "https://example.com/stream", content_type="mp3")
        self.assertEqual(ext, ".mp3")

    def test_from_content_type_m4a(self):
        ext = self.dl._determine_file_extension(
            "https://example.com/stream", content_type="m4a")
        self.assertEqual(ext, ".m4a")

    def test_fallback_when_unknown(self):
        ext = self.dl._determine_file_extension(
            "https://example.com/stream")
        self.assertEqual(ext, ".mp3")

    def test_case_insensitive(self):
        ext = self.dl._determine_file_extension(
            "https://example.com/SONG.FLAC")
        self.assertEqual(ext, ".flac")


if __name__ == "__main__":
    unittest.main()
