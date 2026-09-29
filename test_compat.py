#!/usr/bin/env python3
"""compat.py 的单元测试。

纯标准库，不依赖网络、不依赖 SGLang，任何机器上都能跑：

    python3 -m unittest test_compat -v
    或直接  python3 test_compat.py
"""

from __future__ import annotations

import json
import unittest

import compat

CONTEXT_ERROR = (
    b'{"message":"Requested token count exceeds the model\'s maximum context length of '
    b'262144 tokens. You requested a total of 271206 tokens: 9062 tokens from the input '
    b'messages and 262144 tokens for the completion."}'
)


class TestNormalizeMessages(unittest.TestCase):
    def test_folds_into_existing_system(self):
        payload = {"messages": [
            {"role": "system", "content": "S"},
            {"role": "developer", "content": "D"},
            {"role": "user", "content": "U"},
        ]}
        out = compat.rewrite_payload(payload)["messages"]
        self.assertEqual([m["role"] for m in out], ["system", "user"])
        self.assertEqual(out[0]["content"], "S\n\nD")

    def test_inserts_system_when_absent(self):
        payload = {"messages": [
            {"role": "developer", "content": "D"},
            {"role": "user", "content": "U"},
        ]}
        out = compat.rewrite_payload(payload)["messages"]
        self.assertEqual(out[0], {"role": "system", "content": "D"})
        self.assertEqual(out[1]["role"], "user")

    def test_multiple_developers_merged_in_order(self):
        payload = {"messages": [
            {"role": "developer", "content": "A"},
            {"role": "developer", "content": "B"},
        ]}
        out = compat.rewrite_payload(payload)["messages"]
        self.assertEqual(out, [{"role": "system", "content": "A\n\nB"}])

    def test_multimodal_content_flattened(self):
        payload = {"messages": [{"role": "developer", "content": [
            {"type": "text", "text": "A"},
            {"type": "text", "text": "B"},
        ]}]}
        out = compat.rewrite_payload(payload)["messages"]
        self.assertEqual(out[0]["content"], "A\nB")

    def test_untouched_when_no_developer(self):
        """没有 developer 时不能重建 messages —— 避免无谓开销。"""
        messages = [{"role": "user", "content": "U"}]
        out = compat.rewrite_payload({"messages": messages})["messages"]
        self.assertIs(out, messages)

    def test_non_list_messages_passthrough(self):
        self.assertEqual(compat.normalize_messages("nonsense"), "nonsense")
        self.assertIsNone(compat.normalize_messages(None))


class TestReasoningEffort(unittest.TestCase):
    def test_alias_applied(self):
        out = compat.rewrite_payload({"reasoning_effort": "high"})
        self.assertEqual(out["reasoning_effort"], "xhigh")

    def test_alias_is_case_insensitive(self):
        out = compat.rewrite_payload({"reasoning_effort": "HIGH"})
        self.assertEqual(out["reasoning_effort"], "xhigh")

    def test_other_values_untouched(self):
        for value in ("low", "medium", "xhigh", "minimal"):
            out = compat.rewrite_payload({"reasoning_effort": value})
            self.assertEqual(out["reasoning_effort"], value, value)


class TestOutputConfig(unittest.TestCase):
    def test_only_effort_dropped(self):
        out = compat.rewrite_payload({"output_config": {"effort": "high", "format": "json"}})
        self.assertEqual(out["output_config"], {"format": "json"})

    def test_key_removed_when_empty(self):
        out = compat.rewrite_payload({"output_config": {"effort": "high"}})
        self.assertNotIn("output_config", out)

    def test_untouched_without_effort(self):
        out = compat.rewrite_payload({"output_config": {"format": "json"}})
        self.assertEqual(out["output_config"], {"format": "json"})


class TestPurity(unittest.TestCase):
    def test_does_not_mutate_input(self):
        src = {"messages": [{"role": "developer", "content": "D"}], "reasoning_effort": "high"}
        snapshot = json.dumps(src, sort_keys=True)
        compat.rewrite_payload(src)
        self.assertEqual(json.dumps(src, sort_keys=True), snapshot)

    def test_non_dict_passthrough(self):
        self.assertEqual(compat.rewrite_payload("nope"), "nope")
        self.assertEqual(compat.rewrite_payload(None), None)

    def test_no_rewrite_needed_returns_equal_copy(self):
        src = {"model": "m", "messages": [{"role": "user", "content": "U"}]}
        self.assertEqual(compat.rewrite_payload(src), src)


class TestContextRetryBudget(unittest.TestCase):
    def test_budget_math(self):
        got = compat.context_retry_budget(CONTEXT_ERROR, {"max_tokens": 262144})
        self.assertEqual(got, ("max_tokens", 262144 - 9062 - compat.CONTEXT_RETRY_SAFETY_TOKENS))

    def test_max_completion_tokens_takes_priority(self):
        got = compat.context_retry_budget(
            CONTEXT_ERROR, {"max_completion_tokens": 262144, "max_tokens": 100}
        )
        self.assertEqual(got[0], "max_completion_tokens")

    def test_not_a_context_error(self):
        body = b'{"message":"Unexpected message role.","type":"BadRequest"}'
        self.assertIsNone(compat.context_retry_budget(body, {"max_tokens": 10}))

    def test_empty_body(self):
        self.assertIsNone(compat.context_retry_budget(b"", {"max_tokens": 10}))

    def test_existing_value_already_fits(self):
        """算出来不比原值小就没有重试的意义，必须返回 None。"""
        self.assertIsNone(compat.context_retry_budget(CONTEXT_ERROR, {"max_tokens": 100}))

    def test_input_alone_fills_window(self):
        body = (
            b"maximum context length of 262144 tokens. You requested a total of 262144 "
            b"tokens: 262144 tokens from the input messages and 100 tokens for the completion."
        )
        self.assertIsNone(compat.context_retry_budget(body, {"max_tokens": 100}))

    def test_no_max_tokens_field(self):
        self.assertIsNone(compat.context_retry_budget(CONTEXT_ERROR, {"model": "m"}))

    def test_non_dict_payload(self):
        self.assertIsNone(compat.context_retry_budget(CONTEXT_ERROR, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
