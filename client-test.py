#!/usr/bin/env python3
"""SGLang 网关客户端测试脚本 —— 只依赖 Python 标准库，开箱即用。

用法：
    python3 client-test.py

    # 换地址/换 Key
    BASE_URL=https://llm.example.com API_KEY=sk-qw-xxx python3 client-test.py

    # 换模型
    MODEL=Qwen3.8-27B python3 client-test.py

说明：
    1. 走公网域名时，Cloudflare 的浏览器完整性检查会拦 Python 默认的
       `Python-urllib/x.y` UA，所以这里显式带了 User-Agent 头。
    2. 如果环境里有 HTTP_PROXY，本脚本已主动绕过（ProxyHandler({})）。
    3. 流式测试用「关思考 + 长输出」的方式，才能真正验证逐块到达。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.getenv("BASE_URL", "https://llm.example.com").rstrip("/")
API_KEY = os.getenv("API_KEY", "")
MODEL = os.getenv("MODEL", "Qwen3.8-27B")

UA = "OpenAI/Python 1.60.0"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))


def request(path: str, key: str | None = None, body: dict | None = None, stream: bool = False):
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        return OPENER.open(req, timeout=180), None
    except urllib.error.HTTPError as e:
        return None, e


def read_json(resp) -> dict:
    return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    if not API_KEY:
        print("缺少 API Key。请通过环境变量提供：")
        print()
        print("    API_KEY=sk-qw-xxx python3 client-test.py")
        print()
        print("或先导出：")
        print()
        print("    export API_KEY=sk-qw-xxx")
        print("    export BASE_URL=https://你的域名")
        print()
        print("Key 由网关签发：python cli.py create --name 你的名字")
        return 2

    print(f"目标地址: {BASE_URL}")
    print(f"模型:     {MODEL}")
    print(f"Key:      {API_KEY[:14]}…{API_KEY[-6:]}")
    print()

    # ---------- 1. 健康检查 ----------
    print("1. 健康检查")
    try:
        resp, err = request("/healthz")
        if err:
            check("/healthz", False, f"HTTP {err.code}")
        else:
            d = read_json(resp)
            check("/healthz", d.get("status") == "ok", json.dumps(d, ensure_ascii=False))
    except Exception as e:
        check("/healthz", False, f"连接失败：{e}")

    # ---------- 2. 鉴权拦截 ----------
    print("\n2. 鉴权（无 Key 应被拦截）")
    resp, err = request("/v1/models")
    check("无 Key 返回 401", err is not None and err.code == 401,
          f"HTTP {err.code if err else 200}")

    resp, err = request("/v1/models", key="sk-qw-0000000000000000000000000000000000000000")
    check("错误 Key 返回 401", err is not None and err.code == 401,
          f"HTTP {err.code if err else 200}")

    # ---------- 3. 模型列表 ----------
    print("\n3. 模型列表")
    resp, err = request("/v1/models", key=API_KEY)
    if err:
        check("GET /v1/models", False, f"HTTP {err.code} {err.read().decode()[:120]}")
    else:
        models = read_json(resp).get("data") or []
        ids = [m.get("id") for m in models]
        check("GET /v1/models", bool(models), f"共 {len(models)} 个：{', '.join(map(str, ids))}")
        for m in models:
            print(f"         {m.get('id')}  max_model_len={m.get('max_model_len')}")

    # ---------- 4. 非流式推理 ----------
    print("\n4. 非流式推理")
    t0 = time.time()
    resp, err = request("/v1/chat/completions", key=API_KEY, body={
        "model": MODEL,
        "messages": [{"role": "user", "content": "用一句话解释什么是 API 网关。"}],
        "max_tokens": 300,
        "temperature": 0.6,
    })
    if err:
        check("POST /v1/chat/completions", False, f"HTTP {err.code} {err.read().decode()[:150]}")
    else:
        d = read_json(resp)
        ch = (d.get("choices") or [{}])[0]
        text = (ch.get("message") or {}).get("content") or ""
        u = d.get("usage") or {}
        check("POST /v1/chat/completions", bool(text),
              f"{time.time()-t0:.2f}s  finish={ch.get('finish_reason')}  "
              f"tokens={u.get('completion_tokens')}")
        print(f"         回答：{text.strip()[:80]}")

    # ---------- 5. 流式推理 ----------
    print("\n5. 流式推理（验证逐块到达，未被 Cloudflare 缓冲）")
    body = {
        "model": MODEL,
        "messages": [{"role": "user",
                      "content": "写一段 300 字左右的短文介绍杭州西湖，要求分段落。"}],
        "max_tokens": 1000,
        "temperature": 0.6,
        "stream": True,
        "stream_options": {"include_usage": True},
        # 关掉思考，否则前几秒只产出 reasoning、不产出正文分块，容易误判成被缓冲
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        req = urllib.request.Request(
            BASE_URL + "/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA,
                     "Authorization": f"Bearer {API_KEY}"},
            method="POST",
        )
        t0 = time.time()
        resp = OPENER.open(req, timeout=180)
        first_byte = None
        chunks, chars, usage = [], 0, None
        for raw in resp:
            if first_byte is None:
                first_byte = time.time() - t0
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if p == "[DONE]":
                break
            try:
                obj = json.loads(p)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            for c in obj.get("choices") or []:
                piece = (c.get("delta") or {}).get("content") or ""
                if piece:
                    chars += len(piece)
                    chunks.append(time.time() - t0)

        if not chunks:
            check("流式响应", False, "没收到任何正文分块")
        else:
            span = chunks[-1] - chunks[0]
            check("流式响应", True,
                  f"首字节 {first_byte:.2f}s / {len(chunks)} 块 / {chars} 字符 / "
                  f"正文跨度 {span:.2f}s")
            if span > 1.5:
                print("         逐块实时到达，未被缓冲")
            elif span < 0.5:
                print("         正文几乎同时到达 —— 输出太短或思考未关，证据不足")
            else:
                print("         跨度偏短，建议加大输出长度再测")
            if usage:
                print(f"         usage={usage}")
    except Exception as e:
        check("流式响应", False, f"{type(e).__name__}: {e}")

    # ---------- 汇总 ----------
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
