"""Qwen3.8-27B / SGLang 兼容层。

这个模块原本是一个独立的小代理（`30008 -> 30007`）。后来发现它做的事
本质上是「请求翻译」，而不是「网络转发」——把它内化进网关之后，链路从
四跳减为三跳：

    客户端 -> Cloudflare Tunnel -> 网关(2233) -> SGLang(30007)

它解决三类上游不兼容：

  1. `developer` 角色
     OpenAI 新规范里的 `developer` 角色，SGLang 会直接拒绝：
     `{"message":"Unexpected message role.","type":"BadRequest"}`
     这里把它折叠进第一条 system 消息。

  2. 上下文溢出
     请求的 `input + max_tokens` 超过模型窗口时，SGLang 返回 400。
     这里解析错误文案算出真实可用预算，降 `max_tokens` 重试一次。
     注意：这与网关的「窗口钳制」是两回事——钳制只管 `max_tokens` 本身
     超窗口，管不了「输入已经很长、输出预算还拉满」的组合溢出。

  3. 参数别名
     `reasoning_effort` 的 `high` 在 Qwen 侧对应 `xhigh`；
     Anthropic 风格的 `output_config.effort` 不被识别，需要摘掉。

所有函数都是纯函数，不碰网络、不碰全局状态，方便单独测试。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger("gateway.compat")

# `reasoning_effort` 的取值别名。键是客户端可能发来的，值是上游认识的。
REASONING_EFFORT_ALIASES = {"high": "xhigh"}

# 降 max_tokens 重试时预留的安全边距。
# 上游算出的「可用预算」是按它自己的 tokenizer 数的，与最终实际生成可能
# 有几十个 token 的出入；贴着上限重试容易二次溢出，留 512 个 token 兜底。
CONTEXT_RETRY_SAFETY_TOKENS = int(os.getenv("CONTEXT_RETRY_SAFETY_TOKENS", "512"))

# 输出预算字段。`max_completion_tokens` 是 OpenAI 的新名字，优先级更高。
MAX_TOKENS_FIELDS = ("max_completion_tokens", "max_tokens")


def _content_as_text(content: Any) -> str:
    """把 message.content 压成纯文本。

    content 可能是字符串，也可能是多模态分段数组（OpenAI 的 content parts）。
    折叠 `developer` 角色时必须拿到文本，所以这里统一降维。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(content, (int, float, bool)):
        return str(content)
    return json.dumps(content, ensure_ascii=False)


def _merge_content(left: Any, right: str) -> str:
    """把 right 追加到 left 后面，空值不产生多余空行。"""
    left_text = _content_as_text(left).strip()
    right_text = right.strip()
    if not left_text:
        return right_text
    if not right_text:
        return left_text
    return f"{left_text}\n\n{right_text}"


def normalize_messages(messages: Any) -> Any:
    """把 `developer` 角色折叠进第一条 system 消息。

    SGLang 不认 `developer`，直接 400。折叠策略与 OpenAI 的语义一致：
    developer 指令的优先级高于普通 system，但实际影响很小，统一并进
    第一条 system 即可。

    没有 system 消息时，在最前面插一条；没有任何 developer 时原样返回
    （不复制、不重建，避免无谓开销）。
    """
    if not isinstance(messages, list):
        return messages

    developer_texts: list[str] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "developer":
            text = _content_as_text(msg.get("content")).strip()
            if text:
                developer_texts.append(text)

    if not developer_texts:
        return messages

    merged = "\n\n".join(developer_texts)
    out: list[Any] = []
    inserted = False

    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        role = msg.get("role")
        if role == "developer":
            continue
        if role == "system" and not inserted:
            # 新建 dict 而不是原地改：调用方可能还持有原 payload 做别的用途
            out.append({**msg, "content": _merge_content(msg.get("content"), merged)})
            inserted = True
            continue
        out.append(msg)

    if not inserted:
        out.insert(0, {"role": "system", "content": merged})

    return out


def rewrite_payload(payload: Any) -> Any:
    """对请求体做兼容改写，返回新的 dict（不改动入参）。

    只处理认识的结构，遇到意外类型一律原样放过——兼容层绝不能因为
    自己的解析假设把正常请求搞坏。
    """
    if not isinstance(payload, dict):
        return payload

    out = dict(payload)
    notes: list[str] = []

    # 1. developer -> system
    messages = out.get("messages")
    normalized = normalize_messages(messages)
    if normalized is not messages:
        out["messages"] = normalized
        notes.append("developer->system")

    # 2. reasoning_effort 别名
    effort = out.get("reasoning_effort")
    if isinstance(effort, str):
        alias = REASONING_EFFORT_ALIASES.get(effort.strip().lower())
        if alias is not None:
            out["reasoning_effort"] = alias
            notes.append(f"reasoning_effort:{effort}->{alias}")

    # 3. Anthropic 风格的 output_config.effort —— SGLang 不认识，摘掉
    output_config = out.get("output_config")
    if isinstance(output_config, dict) and "effort" in output_config:
        trimmed = {k: v for k, v in output_config.items() if k != "effort"}
        if trimmed:
            out["output_config"] = trimmed
        else:
            out.pop("output_config", None)
        notes.append("drop output_config.effort")

    if notes:
        logger.info("兼容改写: %s", ", ".join(notes))

    return out


def context_retry_budget(error_body: bytes, payload: Any) -> Optional[tuple[str, int]]:
    """从「上下文溢出」的 400 错误里算出可用的输出预算。

    返回 `(字段名, 建议值)`；不是这种错误、或算出来不比原来小，返回 None。

    上游的文案形如：

        Requested token count exceeds the model's maximum context length of
        262144 tokens. You requested a total of 271206 tokens: 9062 tokens
        from the input messages and 262144 tokens for the completion.

    可用预算 = 窗口 - 输入 - 安全边距。**必须比原值小**，否则重试毫无意义，
    只会把同一个请求再打一遍上游。
    """
    if not isinstance(payload, dict) or not error_body:
        return None

    try:
        message = error_body.decode("utf-8", "replace")
    except Exception:  # pragma: no cover - decode 带 replace 不会抛
        return None

    match = re.search(
        r"maximum context length of (\d+) tokens.*?"
        r"(\d+) tokens from the input messages and (\d+) tokens for the completion",
        message,
        flags=re.DOTALL,
    )
    if not match:
        return None

    context_limit = int(match.group(1))
    input_tokens = int(match.group(2))

    available = context_limit - input_tokens - CONTEXT_RETRY_SAFETY_TOKENS
    if available < 1:
        # 输入本身就把窗口吃满了，降 max_tokens 也救不回来——让上游的报错原样透出
        logger.warning(
            "上下文已无可用输出预算（窗口 %d，输入 %d），不再重试",
            context_limit,
            input_tokens,
        )
        return None

    for field in MAX_TOKENS_FIELDS:
        value = payload.get(field)
        if isinstance(value, int) and value > available:
            return field, available

    return None
