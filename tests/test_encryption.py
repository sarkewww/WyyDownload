"""测试加密模块"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from music_api import CryptoUtils, APIConstants


class TestCryptoUtils(unittest.TestCase):
    def test_hex_digest_basic(self):
        result = CryptoUtils.hex_digest(b'\x00\xff')
        self.assertEqual(result, "00ff")

    def test_hex_digest_empty(self):
        result = CryptoUtils.hex_digest(b'')
        self.assertEqual(result, "")

    def test_hash_digest_known(self):
        result = CryptoUtils.hash_digest("test")
        self.assertIsInstance(result, bytes)
        self.assertEqual(len(result), 16)

    def test_hash_hex_digest_known(self):
        result = CryptoUtils.hash_hex_digest("test")
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 32)

    def test_encrypt_params_format(self):
        payload = {"id": "123", "level": "lossless"}
        result = CryptoUtils.encrypt_params(APIConstants.SONG_URL_V1, payload)
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)
        for c in result:
            self.assertIn(c.lower(), "0123456789abcdef")


if __name__ == "__main__":
    unittest.main()
