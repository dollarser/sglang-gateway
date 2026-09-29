#!/usr/bin/env python3
"""openai_proto.py 的单元测试。

    python3 -m unittest test_openai_proto -v
    或直接  python3 test_openai_proto.py

重点覆盖 `usage_from_response`：它在计费路径上，解析错了会直接导致
配额算不准或被绕过，而且它的输入是不可信的上游响应。
"""

from __future__ import annotations

import unittest

import openai_proto


class TestUsageFromResponse(unittest.TestCase):
    def test_normal_response(self):
        obj = {"model": "Qwen3.8-27B", "usage": {"prompt_tokens": 59, "completion_tokens": 56}}
        self.assertEqual(openai_proto.usage_from_response(obj), (59, 56, "Qwen3.8-27B"))

    def test_missing_usage_returns_zero(self):
        """没有 usage 时返回 0，调用方会退回按字符数粗估，不会漏计费。"""
        self.assertEqual(openai_proto.usage_from_response({"model": "m"}), (0, 0, "m"))

    def test_usage_not_a_dict(self):
        self.assertEqual(openai_proto.usage_from_response({"model": "m", "usage": "x"}), (0, 0, "m"))

    def test_none_token_values_treated_as_zero(self):
        obj = {"usage": {"prompt_tokens": None, "completion_tokens": None}}
        self.assertEqual(openai_proto.usage_from_response(obj), (0, 0, None))

    def test_bogus_token_values_do_not_raise(self):
        """计费是旁路，不能因为它把正常请求搞失败。"""
        obj = {"model": "m", "usage": {"prompt_tokens": "abc", "completion_tokens": 3}}
        self.assertEqual(openai_proto.usage_from_response(obj), (0, 0, "m"))

    def test_non_dict_inputs(self):
        for value in (None, "x", 42, [], b"{}"):
            self.assertEqual(openai_proto.usage_from_response(value), (0, 0, None), value)

    def test_reasoning_tokens_are_inside_completion(self):
        """思维链不单列，`completion_tokens` 已包含它 —— 推理消耗照常计费。"""
        obj = {"usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4096,
            "completion_tokens_details": {"reasoning_tokens": 100},
        }}
        self.assertEqual(openai_proto.usage_from_response(obj), (10, 4096, None))


class TestErrorEnvelope(unittest.TestCase):
    def test_shape(self):
        got = openai_proto.error_envelope("出错了", "invalid_api_key")
        self.assertEqual(got, {"error": {
            "message": "出错了",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }})


class TestMaxTokensFields(unittest.TestCase):
    def test_new_name_takes_priority(self):
        """`max_completion_tokens` 是 OpenAI 的新名字，排在前面表示优先级更高。"""
        self.assertEqual(openai_proto.MAX_TOKENS_FIELDS[0], "max_completion_tokens")
        self.assertIn("max_tokens", openai_proto.MAX_TOKENS_FIELDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
