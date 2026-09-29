"""SGLang API 网关：基于 API Key 的鉴权、限流与用量统计。

链路：
    公网 -> Cloudflare Tunnel -> 本网关(:2233) -> SGLang(:30007)

职责：
    1. 校验 Authorization: Bearer <key>（或 x-api-key）
    2. 每分钟请求数限流 + 每日 token 配额 + 单 key 并发上限
    3. 请求兼容改写（见 sglang_compat.py），然后转发到 SGLang，完整支持 SSE 流式输出
    4. 记录用量，便于后续计费或配额调整

**分层约定**：本文件只放「通用网关」的东西——鉴权、限流、配额、并发、审计、转发。
换一个模型服务还要不要它？要 → 留在这里；不要 → 放 `sglang_compat.py`。
OpenAI 线格式的共享事实（字段名、usage 结构、错误信封）在 `openai_proto.py`，
那个模块存在的理由是「两层都要用，放谁那儿都是错的依赖方向」。

兼容改写原先由一个独立的代理进程承担（`30008 -> 30007`），现已内化到
`sglang_compat.py`，链路由四跳减为三跳。**网关直接连裸 SGLang。**

启动：
    ./run.sh
    或 uvicorn app:app --host 127.0.0.1 --port 2233
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

import openai_proto
import sglang_compat
from openai_proto import MAX_TOKENS_FIELDS
from store import Store, hash_key

try:
    import fcntl
except ImportError:  # Windows 上没有 fcntl，退化为不加锁
    fcntl = None

logger = logging.getLogger("gateway")

# 网关自身的日志默认没有 handler：Python 的 lastResort 只兜 WARNING 及以上，
# 所以 logger.info 全部被丢掉。这会让「启动参数」「模型窗口探测结果」这类
# 关键信息看不到，排查时很被动。这里显式配一个。
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx 每个请求都打一条 INFO，量大了会把有用的日志淹掉
    logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------- 配置 ----------------

SGLANG_BASE_URL = os.getenv("SGLANG_BASE_URL", "http://127.0.0.1:30007").rstrip("/")
SGLANG_API_KEY = os.getenv("SGLANG_API_KEY", "")
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gateway.db")
GATEWAY_DB = os.getenv("GATEWAY_DB", _DEFAULT_DB)
# 请求体上限：默认 60MB（严格低于 64MB，兼顾多模态高清图/短视频与网络稳定性）
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(60 * 1024 * 1024)))
INJECT_STREAM_USAGE = os.getenv("INJECT_STREAM_USAGE", "1") not in ("0", "false", "no")
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

# 兼容改写开关。默认开启；上游升级后不再需要时可以单独关掉，不用改代码。
COMPAT_REWRITE = os.getenv("COMPAT_REWRITE", "1") not in ("0", "false", "no")
# 上下文溢出重试开关。关掉后上游的 400 会原样透出，不再自动降 max_tokens。
COMPAT_CONTEXT_RETRY = os.getenv("COMPAT_CONTEXT_RETRY", "1") not in ("0", "false", "no")

# 粗估 token 的兜底系数：拿不到 usage 时按字符数折算
CHARS_PER_TOKEN = float(os.getenv("CHARS_PER_TOKEN", "2.0"))

# 只允许转发到这些推理端点。白名单是防路径穿越的关键：
# 缺少它时，持有合法 Key 的客户端可以用 /v1/../flush_cache 打中 SGLang 的管理端点。
DEFAULT_ALLOWED_PATHS = "chat/completions,completions,embeddings,models,rerank,score"
ALLOWED_PATHS = {
    p.strip() for p in os.getenv("ALLOWED_PATHS", DEFAULT_ALLOWED_PATHS).split(",") if p.strip()
}

# 单次请求的输出上限。
#   0  = 不限制，交由「模型上下文窗口」和「该 Key 当日剩余配额」两级约束（推荐）
#   >0 = 硬上限，超出会被钳制
# 钳制的意义是「让请求正常返回内容」，而不是「规避一个看不懂的报错」：
# SGLang 对超过 max_model_len 的 max_tokens 会返回 400，报错文案本身是清楚的
# （`max_completion_tokens is too large: 999999. This model supports at most 262144
# completion tokens.`），但对客户端来说这仍是一次失败。钳到窗口内就能拿到结果。
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "0"))

# 模型上下文窗口。0 表示启动时自动从上游 /v1/models 的 max_model_len 探测。
# 探测到之后，超过窗口的 max_tokens 会被钳到窗口大小，用户拿到的是正常响应，
# 而不是一个 400 报错。
MODEL_MAX_LEN = int(os.getenv("MODEL_MAX_LEN", "0"))

# 全局并发上限。严格按整台机器 SGLang 的承载能力匹配（后端 --max-running-requests 设为 4 时对齐设为 4）。
# 避免过量并发冲垮 GPU 显存与调度队列；当突发超过 4 时网关安全拦截并返回 503，防止服务雪崩。
GLOBAL_MAX_CONCURRENT = int(os.getenv("GLOBAL_MAX_CONCURRENT", "4"))

# 单个客户端每分钟允许的鉴权失败次数。放宽至 60，避免同 NAT 局域网被误伤。
AUTH_FAIL_MAX = int(os.getenv("AUTH_FAIL_MAX", "60"))

# 单实例锁文件。本网关设计为单 worker 运行：限流状态虽已持久化到 SQLite，
# 但并发计数仍在内存里，多实例会让全局并发上限实际翻倍。用文件锁挡住误启动的第二个实例。
SINGLETON_LOCK = os.getenv("SINGLETON_LOCK", GATEWAY_DB + ".lock")

store = Store(GATEWAY_DB)


# ---------------- 限流与并发控制 ----------------

# 限流窗口统一为 60 秒。
# 状态存在 SQLite 里而不是内存，这样网关重启不会把计数清零——否则攻击者只要
# 想办法触发一次重启，限流窗口就被重置了。代价是每个请求多几次本地数据库往返，
# 对 LLM 这种秒级响应的服务完全可以忽略。
RATE_WINDOW = 60.0


def _check_rate(scope: str, limit: int) -> tuple[bool, int]:
    """滑动窗口限流检查，返回 (是否放行, 建议重试秒数)。放行后需自行 record_event。"""
    return store.check_rate(scope, limit, RATE_WINDOW)


def _record_auth_fail(ip: str) -> None:
    store.record_event(f"a:{ip}")


class ConcurrencyGuard:
    """同时在两个维度限制在途请求：单 Key 上限 + 全局上限。

    只做单 Key 限制是不够的。每个 Key 单独看都在限额内，但几十个 Key 叠加起来
    仍能把 GPU 打满，所以必须有一道全局闸门。

    这个计数**不**持久化：进程重启后在途请求本就不存在了，计数归零才是正确语义。
    """

    def __init__(self) -> None:
        self._counts: dict[int, int] = defaultdict(int)
        self._global = 0

    def acquire(self, key_id: int, limit: int) -> str:
        """返回 'ok' / 'global_full' / 'key_full'。limit <= 0 表示不限制单 Key 并发。"""
        if self._global >= GLOBAL_MAX_CONCURRENT:
            return "global_full"
        if limit > 0 and self._counts[key_id] >= limit:
            return "key_full"
        self._counts[key_id] += 1
        self._global += 1
        return "ok"

    def release(self, key_id: int) -> None:
        if self._counts.get(key_id):
            self._counts[key_id] -= 1
            self._global = max(0, self._global - 1)


guard = ConcurrencyGuard()


# ---------------- 应用 ----------------


def _acquire_singleton_lock(path: str):
    """确保同一份数据库只被一个网关进程使用。

    没有这把锁时，误启动第二个实例不会报任何错，但全局并发上限会静默翻倍，
    排查起来非常困难——所以宁可启动失败。
    """
    if fcntl is None:
        return None
    # 用 "a+" 打开：不存在则创建，存在则**不截断**。
    # 这里不能用 "w"——第二个实例尝试启动时会把锁文件清空，虽然锁本身基于文件描述符
    # 不受影响，但正在运行的实例留下的 PID 就被抹掉了，排查时反而添乱。
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(
            f"另一个网关实例正在使用 {GATEWAY_DB}（锁文件 {path}）。"
            "本网关设计为单 worker 运行，请先停掉已有实例。"
        ) from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


async def _fetch_model_max_len(client: httpx.AsyncClient) -> int:
    """启动时从上游 `/v1/models` 取模型上下文窗口，作为输出长度的天然上限。

    这里只负责**发请求**；怎么从响应里把窗口抠出来属于服务栈特有的知识，
    在 `sglang_compat.parse_model_max_len` 里。

    为什么要探测它：SGLang 对 max_tokens > max_model_len 的请求返回 400
    （报错文案清楚，但请求毕竟失败了）。钳到窗口大小之后，同样的请求会正常返回内容。

    探测失败不致命——退回「不做窗口钳制」，并在日志里说清楚。
    """
    headers = {}
    if SGLANG_API_KEY:
        headers["authorization"] = f"Bearer {SGLANG_API_KEY}"
    try:
        resp = await client.get(
            f"{SGLANG_BASE_URL}/v1/models", headers=headers, timeout=10.0
        )
        resp.raise_for_status()
        return sglang_compat.parse_model_max_len(resp.json())
    except Exception:
        logger.warning(
            "探测模型上下文窗口失败，本次启动不做窗口钳制。"
            "若上游稍后才就绪，可重启网关或显式设置 MODEL_MAX_LEN",
            exc_info=True,
        )
    return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    lock = _acquire_singleton_lock(SINGLETON_LOCK)
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=600.0, write=120.0, pool=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )
    app.state.model_max_len = MODEL_MAX_LEN or await _fetch_model_max_len(app.state.client)
    logger.info(
        "输出长度上限：MAX_OUTPUT_TOKENS=%s，模型上下文窗口=%s",
        MAX_OUTPUT_TOKENS or "不限",
        app.state.model_max_len or "未知",
    )
    try:
        yield
    finally:
        await app.state.client.aclose()
        store.close()
        if lock is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock.close()


# 三个自动生成的文档端点全部关掉。
# docs_url / redoc_url 默认给 /docs 和 /redoc，openapi_url 默认给 /openapi.json。
# /openapi.json 无需鉴权就能拿到完整路由表，连 proxy() 的 docstring 都会一起吐出去，
# 等于把内部实现思路交给扫描者 —— 而且它不经过 /v1/{path}，白名单管不到。
app = FastAPI(
    title="SGLang API Gateway",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _error(status: int, message: str, code: str, headers: Optional[dict[str, str]] = None):
    """返回 OpenAI 风格的错误体，保证各类 SDK 能正确解析。

    信封结构在 `openai_proto`（两层共用），这里只负责套上 HTTP 状态码与响应头。
    """
    return JSONResponse(
        status_code=status,
        content=openai_proto.error_envelope(message, code),
        headers=headers or {},
    )


def _client_ip(request: Request) -> str:
    """取真实客户端 IP，用于鉴权失败计数。

    Cloudflare 会把来源 IP 写入 CF-Connecting-IP 并覆盖客户端自带的同名头。
    网关只监听回环、流量必经 Tunnel，所以这个头可信；直连网关时无法伪造出
    有价值的身份，最多只能影响自己被限流的粒度。
    """
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    return request.client.host if request.client else "unknown"


def _extract_key(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    xk = request.headers.get("x-api-key", "").strip()
    return xk or None


class AuditContext:
    """一次请求的审计上下文。

    贯穿鉴权、转发、流式收尾三个阶段，最后统一落一条记录。
    失败路径同样会写入——被拒绝的请求恰恰是安全审计最关心的部分。
    """

    __slots__ = (
        "ip", "method", "path", "user_agent", "started",
        "key_id", "key_prefix", "model", "pt", "ct", "written",
        "remaining_quota",
    )

    def __init__(self, request: Request, path: str) -> None:
        self.ip = _client_ip(request)
        self.method = request.method
        self.path = path
        self.user_agent = (request.headers.get("user-agent") or "")[:200]
        self.started = time.monotonic()
        self.key_id: Optional[int] = None
        self.key_prefix: Optional[str] = None
        self.model: Optional[str] = None
        self.pt = 0
        self.ct = 0
        self.written = False
        # 该 Key 当日剩余 token 配额，由 _authorize 填充。
        # 用来钳制单次请求的输出——否则一个 max_tokens 拉满的请求就能把当天配额打穿。
        self.remaining_quota = 0

    def finish(self, status: int, outcome: str) -> None:
        """落一条审计记录。

        重复调用只生效一次；任何异常都不得影响主流程——审计是旁路，
        不能因为它写失败就让用户的请求跟着失败。
        """
        if self.written:
            return
        self.written = True
        try:
            store.record_audit(
                ip=self.ip,
                key_id=self.key_id,
                key_prefix=self.key_prefix,
                method=self.method,
                path=self.path,
                model=self.model,
                status=status,
                outcome=outcome,
                prompt_tokens=self.pt,
                completion_tokens=self.ct,
                latency_ms=int((time.monotonic() - self.started) * 1000),
                user_agent=self.user_agent,
            )
        except Exception:
            logger.exception("写入审计日志失败")


async def _authorize(request: Request, ctx: AuditContext):
    """校验 key 并执行限流、配额、并发检查。

    成功返回 (key 记录, None)；失败返回 (None, 错误响应)。
    无论成败都把结果写进 ctx，由调用方统一落审计日志。
    """
    ip = ctx.ip

    if not _check_rate(f"a:{ip}", AUTH_FAIL_MAX)[0]:
        ctx.finish(429, "auth_flood")
        return None, _error(
            429,
            "鉴权失败次数过多，请稍后再试。",
            "too_many_failed_attempts",
            {"Retry-After": str(int(RATE_WINDOW))},
        )

    raw = _extract_key(request)
    if not raw:
        _record_auth_fail(ip)
        ctx.finish(401, "auth_missing")
        return None, _error(
            401, "缺少 API Key，请在 Authorization 头中提供 Bearer 令牌。", "invalid_api_key",
            {"WWW-Authenticate": "Bearer"},
        )

    record = store.get_by_hash(hash_key(raw))
    if record is None:
        _record_auth_fail(ip)
        logger.warning("鉴权失败 ip=%s", ip)
        ctx.finish(401, "auth_invalid")
        return None, _error(401, "API Key 无效。", "invalid_api_key")
    if record["revoked"]:
        ctx.finish(403, "auth_revoked")
        return None, _error(403, "API Key 已被吊销。", "key_revoked")
    if record["expires_at"] and record["expires_at"] < time.strftime("%Y-%m-%d %H:%M:%S"):
        ctx.finish(403, "auth_expired")
        return None, _error(403, "API Key 已过期。", "key_expired")

    key_id = record["id"]
    ctx.key_id = key_id
    ctx.key_prefix = record["key_prefix"]

    # rpm_limit <= 0 视为不限频（免去滑动窗口检查与数据库写开销，适用于高频 Agent）
    if record["rpm_limit"] > 0:
        ok, retry_after = _check_rate(f"k:{key_id}", record["rpm_limit"])
        if not ok:
            ctx.finish(429, "rate_limited")
            return None, _error(
                429,
                f"请求过于频繁，限制为每分钟 {record['rpm_limit']} 次，请 {retry_after} 秒后重试。",
                "rate_limit_exceeded",
                {"Retry-After": str(retry_after)},
            )
        store.record_event(f"k:{key_id}")

    used = store.used_tokens_today(key_id)
    # daily_tokens <= 0 视为不限量（与 MAX_OUTPUT_TOKENS=0 的约定一致）
    if record["daily_tokens"] > 0 and used >= record["daily_tokens"]:
        ctx.finish(429, "quota_exceeded")
        return None, _error(
            429,
            f"今日 token 配额已用尽（{used}/{record['daily_tokens']}），明日 00:00 重置。",
            "quota_exceeded",
        )
    ctx.remaining_quota = (
        record["daily_tokens"] - used if record["daily_tokens"] > 0 else 0
    )

    outcome = guard.acquire(key_id, record["max_concurrent"])
    if outcome == "global_full":
        ctx.finish(429, "service_busy")
        return None, _error(429, "服务当前请求量已满，请稍后重试。", "service_busy")
    if outcome == "key_full":
        ctx.finish(429, "concurrency_limited")
        return None, _error(
            429,
            f"并发请求数已达上限（{record['max_concurrent']}），请等待当前请求完成。",
            "concurrency_limit_exceeded",
        )

    return record, None


def _json_bytes(obj: Any) -> bytes:
    """紧凑序列化请求体。

    `ensure_ascii=False` 让中文按 UTF-8 直出，比 `\\uXXXX` 转义省一半字节；
    分隔符去掉空格，长 messages 数组能省下可观体积。
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _record(key_id: int, model, pt: int, ct: int, status: int, started: float) -> None:
    """写入用量。拿不到真实 usage 时按字符数粗估，保证配额不会被绕过。"""
    store.record_usage(
        key_id=key_id,
        model=model,
        prompt_tokens=max(pt, 0),
        completion_tokens=max(ct, 0),
        status=status,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


# ---------------- 路由 ----------------


def _safe_path(path: str) -> Optional[str]:
    """校验转发路径，只放行白名单内的推理端点。

    必须拦住的几类输入：
      ../..        路径穿越，越出 /v1/ 后会打中 SGLang 的管理端点
      %2e%2e%2f    编码后的穿越（Starlette 会先解码一层，这里再兜一层）
      以 / 开头     绝对路径
      ?  # 空字节  查询串或片段残留
    """
    if not path or path.startswith("/"):
        return None
    if ".." in path or "\\" in path:
        return None
    if any(ch in path for ch in ("?", "#", "\x00")):
        return None
    decoded = unquote(path)
    if ".." in decoded or decoded.startswith("/"):
        return None
    if path not in ALLOWED_PATHS:
        return None
    return path


@app.get("/healthz")
async def healthz():
    # 不返回上游地址，避免向公网泄露内网拓扑
    return {"status": "ok"}


async def _read_body(request: Request) -> Optional[bytes]:
    """读取请求体，超限返回 None。

    先看 `content-length` 再读，避免把超大 body 整个读进内存之后才拒绝。
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = await request.body()
    return None if len(body) > MAX_BODY_BYTES else body


def _output_cap(ctx: AuditContext, model_max_len: int) -> Optional[int]:
    """三级输出上限取最小值。三级都是 0 时返回 None，表示不钳制。

      1. `MAX_OUTPUT_TOKENS` —— 全局硬上限，0 表示不设
      2. 模型上下文窗口       —— 启动时自动探测
      3. 当日剩余配额         —— 防止单次请求把当天额度打穿
    """
    caps = []
    if MAX_OUTPUT_TOKENS > 0:
        caps.append(MAX_OUTPUT_TOKENS)
    if model_max_len:
        caps.append(model_max_len)
    if ctx.remaining_quota > 0:
        caps.append(ctx.remaining_quota)
    return min(caps) if caps else None


def _prepare_upstream_body(
    raw_body: bytes, ctx: AuditContext, model_max_len: int
) -> tuple[bytes, dict[str, Any]]:
    """把客户端请求体加工成「要发给上游的」字节，返回 `(body, payload)`。

    三步，顺序不能换：

      1. 兼容改写（`sglang_compat`）—— 可能重建整个 messages 数组
      2. 注入 `stream_options`       —— 流式请求才加，好从数据流里抓 usage
      3. 输出长度钳制                —— 三级上限取最小值

    解析不出 dict（空体、非 JSON、JSON 数组）时原样放行，不做任何加工。
    """
    payload: dict[str, Any] = {}
    if raw_body:
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                payload = parsed
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    if not payload:
        return raw_body, payload

    if COMPAT_REWRITE:
        payload = sglang_compat.rewrite_payload(payload)

    if payload.get("stream") and INJECT_STREAM_USAGE and "stream_options" not in payload:
        payload["stream_options"] = {"include_usage": True}

    cap = _output_cap(ctx, model_max_len)
    if cap is not None:
        for field in MAX_TOKENS_FIELDS:
            value = payload.get(field)
            if isinstance(value, int) and value > cap:
                payload[field] = cap

    return _json_bytes(payload), payload


def _upstream_headers(request: Request) -> dict[str, str]:
    """构造发给上游的请求头。

    只带必要的三项。客户端自带的其余头一律不透传——尤其是它自己的
    `Authorization`，那是给网关用的，不能漏给上游。
    """
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    if SGLANG_API_KEY:
        headers["authorization"] = f"Bearer {SGLANG_API_KEY}"
    if accept := request.headers.get("accept"):
        headers["accept"] = accept
    return headers


async def _send_with_context_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    body: bytes,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> tuple[Optional[httpx.Response], bytes, dict[str, Any]]:
    """发送上游请求；命中「上下文溢出」时降 max_tokens 重试一次。

    返回 `(响应, 需要原样回传的错误体, 最终 payload)`：

      - `响应 is not None` → 正常路径，调用方按流式 / 非流式处理
      - `响应 is None`     → 上游拒绝且不该重试，**必须**把错误体原样回给客户端

    上游对「输入 + max_tokens 超过模型窗口」返回 400，而网关的窗口钳制管不了
    这种组合溢出（输入本身已经很长），只能从错误文案反算可用预算再试一次。

    这里曾经有个 bug：旧实现不管重不重试都先 `await resp.aread()` 把 body 读掉，
    不重试时后续 `aiter_raw()` 会抛 StreamConsumed，客户端收到一个**空 body 的
    400**，完全看不出原因。所以 body 只在两条**互斥**分支里被消费：要么拿去算
    预算并重试，要么原样回传。**不要在这里加第三种消费方式。**
    """

    async def send(current_body: bytes) -> httpx.Response:
        # 用 send(stream=True) 而不是 client.stream 上下文管理器：
        # 生成器要在管理器退出之后继续读，生命周期必须由调用方掌控。
        return await client.send(
            client.build_request(method, url, content=current_body, headers=headers),
            stream=True,
        )

    resp = await send(body)

    # 只有这个状态码才值得读 body。其余情况要原样流转给客户端，不能提前消费。
    if resp.status_code != sglang_compat.CONTEXT_OVERFLOW_STATUS:
        return resp, b"", payload
    if not COMPAT_CONTEXT_RETRY or not payload:
        return resp, b"", payload

    error_body = await resp.aread()
    await resp.aclose()

    plan = sglang_compat.plan_context_retry(error_body, payload)
    if plan is None:
        # 不是上下文溢出，或输入已占满窗口无预算可降 → 交给调用方原样透出
        return None, error_body, payload

    payload, note = plan
    logger.info("上下文溢出：%s，重试一次", note)
    return await send(_json_bytes(payload)), b"", payload


async def _relay_non_stream(
    resp: httpx.Response,
    request: Request,
    key_id: int,
    payload: dict[str, Any],
    started: float,
    release_once,
    ctx: AuditContext,
) -> Response:
    """非流式：读完整响应 → 计费落账 → 构造响应。"""
    try:
        content = await resp.aread()
    finally:
        await resp.aclose()

    pt = ct = 0
    model = None
    try:
        pt, ct, model = openai_proto.usage_from_response(json.loads(content))
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    # 只有「会产生推理开销」的 POST 才做兜底估算：
    # 跳过 GET /v1/models，也跳过 4xx——请求失败时没有推理发生，不该扣配额。
    if pt == 0 and ct == 0 and request.method == "POST" and resp.status_code < 400:
        ct = int(len(content) / CHARS_PER_TOKEN)

    ctx.model = model or payload.get("model")
    ctx.pt = pt
    ctx.ct = ct

    _record(key_id, model or payload.get("model"), pt, ct, resp.status_code, started)
    release_once()
    ctx.finish(resp.status_code, "ok" if resp.status_code < 400 else "upstream_error")
    return Response(
        content=content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def proxy(path: str, request: Request):
    """转发入口：鉴权 → 加工请求 → 发送（含溢出重试）→ 回传。

    这里只做编排与收尾。每一步的实现都在上面几个独立函数里，这样每个环节
    都能单独测，也不用在一个函数里同时理解限流、改写、重试和计费。
    """
    if request.method == "OPTIONS":
        return Response(status_code=204)

    ctx = AuditContext(request, path)

    safe_path = _safe_path(path)
    if safe_path is None:
        logger.warning("拦截到非白名单路径: %s", path)
        ctx.finish(404, "path_blocked")
        return _error(404, "请求的接口不存在。", "not_found")

    record, err = await _authorize(request, ctx)
    if err is not None:
        return err

    key_id = record["id"]
    started = time.monotonic()
    released = False

    def release_once() -> None:
        nonlocal released
        if not released:
            released = True
            guard.release(key_id)

    try:
        raw_body = await _read_body(request)
        if raw_body is None:
            release_once()
            ctx.finish(413, "payload_too_large")
            return _error(413, "请求体过大。", "payload_too_large")

        body, payload = _prepare_upstream_body(
            raw_body, ctx, request.app.state.model_max_len
        )

        resp, error_body, payload = await _send_with_context_retry(
            request.app.state.client,
            request.method,
            f"{SGLANG_BASE_URL}/v1/{safe_path}",
            body,
            _upstream_headers(request),
            payload,
        )

        if resp is None:
            # 上游拒绝且不该重试：原样回传它的错误说明，让客户端看到
            # `max_completion_tokens is too large: 999999. ...` 这类明确信息。
            # 此时响应 body 已被读掉，绝不能再走下面的流式 / 非流式分支。
            status = sglang_compat.CONTEXT_OVERFLOW_STATUS
            _record(key_id, payload.get("model"), 0, 0, status, started)
            release_once()
            ctx.model = payload.get("model")
            ctx.finish(status, "upstream_error")
            return Response(content=error_body, status_code=status, media_type="application/json")

        if payload.get("stream"):
            # 流式的审计记录在流结束后由 _stream_and_record 落，这样才带得上 token 数
            return StreamingResponse(
                _stream_and_record(resp, key_id, record, started, release_once, ctx),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "text/event-stream"),
            )

        return await _relay_non_stream(
            resp, request, key_id, payload, started, release_once, ctx
        )

    except httpx.ConnectError:
        release_once()
        ctx.finish(502, "upstream_unreachable")
        return _error(502, "无法连接到 SGLang 后端服务。", "upstream_unreachable")
    except httpx.TimeoutException:
        release_once()
        ctx.finish(504, "upstream_timeout")
        return _error(504, "上游服务响应超时。", "upstream_timeout")
    except Exception:  # noqa: BLE001 - 兜底，避免异常导致并发计数泄漏
        # 异常详情只写服务端日志，不回传给客户端，避免泄露内网路径与库版本
        logger.exception("网关内部错误")
        release_once()
        ctx.finish(500, "error")
        return _error(500, "网关内部错误，请稍后重试。", "internal_error")


async def _stream_and_record(
    resp: httpx.Response,
    key_id: int,
    record: dict[str, Any],
    started: float,
    release_once,
    ctx: AuditContext,
) -> AsyncIterator[bytes]:
    """逐块转发 SSE，同时从数据流里抓取 usage 用于计费。"""
    pt = ct = 0
    model: Optional[str] = record.get("name")
    char_count = 0
    status = resp.status_code
    completed = False
    upstream_broken = False

    try:
        try:
            async for chunk in resp.aiter_bytes():
                char_count += len(chunk)
                if b'"usage"' in chunk:
                    text = chunk.decode("utf-8", "ignore")
                    for line in text.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        p, c, m = openai_proto.usage_from_response(obj)
                        if p or c:
                            pt, ct = p, c
                        if m:
                            model = m
                yield chunk
            completed = True
        except Exception:
            # 注意这里只捕 Exception：客户端断连抛的是 GeneratorExit，
            # 它不属于 Exception，会直接穿过这里，从而与「上游中断」区分开。
            upstream_broken = True
            raise
    finally:
        # 关键顺序：先落定并发位和用量，再做可能被取消的收尾动作。
        # 客户端断连时这个生成器会被取消，如果先 await 清理，取消异常可能让后续代码
        # 不再执行，导致并发计数永久泄漏——该 Key 从此再也发不出请求，只能重启网关。
        try:
            if pt == 0 and ct == 0 and status < 400:
                ct = int(char_count / CHARS_PER_TOKEN)
            _record(key_id, model, pt, ct, status, started)
        except Exception:
            logger.exception("记录用量失败")
        finally:
            release_once()

        # 审计记录放在最后落，这样才带得上本次流式产生的 token 数。
        # 区分三种收尾：正常完成 / 客户端中途断连 / 上游中断。
        # 客户端断连值得单独标记——可能是客户端 bug，也可能是拿到一部分就走的抓取行为。
        if completed:
            outcome = "ok" if status < 400 else "upstream_error"
        elif upstream_broken:
            outcome = "stream_broken"
        else:
            # 客户端中途断连。状态码记 499（nginx 对「客户端提前关闭连接」的约定），
            # 这样在审计表里一眼就能和正常完成的 200 区分开。
            outcome = "client_abort"
            status = 499
        ctx.model = model
        ctx.pt = pt
        ctx.ct = ct
        ctx.finish(status, outcome)

        try:
            await resp.aclose()
        except BaseException:
            pass
