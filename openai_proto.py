"""OpenAI 线格式（wire format）的共享事实。

**这个模块为什么存在**

有些东西同时被「通用网关层」（`app.py`）和「模型专用层」（`sglang_compat.py`）需要，
放在任何一层都会造成错误的依赖：

  - 放 `app.py`        → `sglang_compat` 得反过来 import `app`，循环依赖
  - 放 `sglang_compat` → 通用层反向依赖专用层，将来上游修好、想删掉
                         兼容层时会连带把通用逻辑弄挂

所以单独拎出来，谁都不欠谁。

**这里只放「OpenAI 规范规定的」东西**，不放任何厂商特有的：

  - `MAX_TOKENS_FIELDS`  —— 请求里的输出预算字段名（`max_tokens` 是旧名，
                            `max_completion_tokens` 是新名，后者优先级更高）
  - `usage_from_response` —— 响应里 `usage` 的结构
  - `error_envelope`      —— 错误响应的信封结构

SGLang 特有的东西（`max_model_len` 字段、`developer` 角色、溢出报错文案）
一律在 `sglang_compat.py`，不要挪进来。
"""

from __future__ import annotations

from typing import Any, Optional

# 输出预算字段。两个都是 OpenAI 规范里的名字，`max_completion_tokens` 更新、优先。
MAX_TOKENS_FIELDS = ("max_completion_tokens", "max_tokens")

# 错误信封里的固定 type 值。OpenAI 对 4xx 一律用这个。
INVALID_REQUEST_ERROR = "invalid_request_error"


def usage_from_response(obj: Any) -> tuple[int, int, Optional[str]]:
    """从响应体里取 `(prompt_tokens, completion_tokens, model)`。

    取不到就返回 0 —— 调用方会退回按字符数粗估，不会因此漏计费。
    结构不符合预期时一律当「没有 usage」处理，绝不能抛异常：
    这是计费路径上的旁路，不能因为它把正常请求搞失败。
    """
    if not isinstance(obj, dict):
        return 0, 0, None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return 0, 0, obj.get("model")
    try:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return 0, 0, obj.get("model")
    return prompt, completion, obj.get("model")


def error_envelope(message: str, code: str) -> dict[str, Any]:
    """构造 OpenAI 风格的错误体，保证各类 SDK 能正确解析出 message。

    返回纯 dict，不依赖任何 Web 框架——`app.py` 自己套一层 JSONResponse。
    """
    return {
        "error": {
            "message": message,
            "type": INVALID_REQUEST_ERROR,
            "code": code,
        }
    }
