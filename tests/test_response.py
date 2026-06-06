"""测试响应格式"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from main import APIResponse


class TestAPIResponse(unittest.TestCase):
    def test_success_without_data(self):
        resp, code = APIResponse.success(message="ok")
        self.assertEqual(code, 200)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["status"], 200)
        self.assertEqual(resp["message"], "ok")
        self.assertNotIn("data", resp)

    def test_success_with_data(self):
        resp, code = APIResponse.success(data={"id": 1})
        self.assertEqual(code, 200)
        self.assertEqual(resp["data"], {"id": 1})

    def test_success_custom_code(self):
        resp, code = APIResponse.success(message="created", status_code=201)
        self.assertEqual(code, 201)
        self.assertEqual(resp["status"], 201)

    def test_error_basic(self):
        resp, code = APIResponse.error("not found", 404)
        self.assertEqual(code, 404)
        self.assertFalse(resp["success"])
        self.assertEqual(resp["status"], 404)
        self.assertEqual(resp["message"], "not found")

    def test_error_with_code(self):
        resp, code = APIResponse.error("bad", 400, error_code="INVALID_PARAM")
        self.assertEqual(resp["error_code"], "INVALID_PARAM")

    def test_error_default_code(self):
        resp, code = APIResponse.error("bad")
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
