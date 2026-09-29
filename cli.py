#!/usr/bin/env python3
"""API Key 管理命令行工具。

用法：
    python cli.py create --name alice --rpm 60 --daily-tokens 2000000 --expires 90d
    python cli.py list
    python cli.py revoke 3
    python cli.py revoke sk-qw-a1b2
    python cli.py usage --days 7
    python cli.py audit --days 1 --summary
    python cli.py audit --days 1 --outcome auth_invalid
    python cli.py cleanup --keep-days 30
"""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata

from store import Store


def _display_width(text: object) -> int:
    """按终端显示宽度计算，中日韩全角字符算 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(text))


def _pad(text: object, width: int) -> str:
    """左对齐补齐到指定显示宽度，保证中文表头与数据对齐。

    超宽时截断并加省略号。审计日志里的路径长度不可控（攻击者可以发超长路径），
    不截断的话整行会错位，后续列全部跟着漂移。
    """
    text = str(text)
    current = _display_width(text)
    if current <= width:
        return text + " " * (width - current)
    if width <= 1:
        return "…"[:width]
    out: list[str] = []
    used = 0
    # 留出 1 列给省略号、再留 1 列做列间分隔，否则截断后的内容会正好占满列宽，
    # 和右侧一列贴在一起（…404）。
    for ch in text:
        ch_width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + ch_width > width - 2:
            break
        out.append(ch)
        used += ch_width
    return "".join(out) + "…" + " " * (width - used - 1)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_quota(n: int) -> str:
    """配额显示。<=0 表示不限量，别显示成「0 tokens/天」那样容易被误解。"""
    return "不限" if n <= 0 else _fmt_tokens(n)


def _fmt_rpm(n: int) -> str:
    return "不限" if n <= 0 else f"{n} 次/分钟"


def _fmt_concurrent(n: int) -> str:
    return "不限" if n <= 0 else str(n)


def cmd_create(store: Store, args: argparse.Namespace) -> int:
    raw, row = store.create_key(
        name=args.name,
        rpm_limit=args.rpm,
        daily_tokens=args.daily_tokens,
        max_concurrent=args.max_concurrent,
        expires=args.expires,
        note=args.note,
    )
    print("创建成功。以下密钥只显示这一次，请立即保存：\n")
    print(f"  密钥    {raw}")
    print(f"  编号    {row['id']}")
    print(f"  名称    {row['name']}")
    print(f"  限流    {_fmt_rpm(row['rpm_limit'])}")
    print(f"  配额    {_fmt_quota(row['daily_tokens'])} tokens/天")
    print(f"  并发    {_fmt_concurrent(row['max_concurrent'])}")
    print(f"  过期    {row['expires_at'] or '永不过期'}")
    print("\n客户端配置：")
    print(f"  base_url = <你的域名>/v1")
    print(f"  api_key  = {raw}")
    return 0


def cmd_list(store: Store, args: argparse.Namespace) -> int:
    rows = store.list_keys(include_revoked=args.all)
    if not rows:
        print("暂无密钥。")
        return 0
    cols = [("ID", 5), ("前缀", 15), ("名称", 16), ("限流", 10), ("日配额", 11), ("过期", 22), ("状态", 8)]
    print("".join(_pad(name, width) for name, width in cols))
    print("-" * sum(width for _, width in cols))
    for r in rows:
        values = [
            r["id"],
            r["key_prefix"],
            r["name"],
            "不限" if r["rpm_limit"] <= 0 else f"{r['rpm_limit']}/min",
            _fmt_quota(r["daily_tokens"]),
            r["expires_at"] or "永不过期",
            "已吊销" if r["revoked"] else "有效",
        ]
        print("".join(_pad(v, w) for v, (_, w) in zip(values, cols)))
    return 0


def cmd_revoke(store: Store, args: argparse.Namespace) -> int:
    count = store.revoke_key(args.identifier)
    if count:
        print(f"已吊销 {count} 个密钥。")
        return 0
    print("未找到匹配的有效密钥。", file=sys.stderr)
    return 1


def cmd_usage(store: Store, args: argparse.Namespace) -> int:
    rows = store.usage_summary(days=args.days)
    if not rows:
        print(f"最近 {args.days} 天没有用量记录。")
        return 0
    cols = [("日期", 12), ("ID", 5), ("名称", 16), ("请求数", 9), ("输入", 11), ("输出", 11)]
    print("".join(_pad(name, width) for name, width in cols))
    print("-" * sum(width for _, width in cols))
    total_req = total_pt = total_ct = 0
    for r in rows:
        total_req += r["requests"]
        total_pt += r["prompt_tokens"]
        total_ct += r["completion_tokens"]
        values = [
            r["date"],
            r["id"],
            r["name"],
            r["requests"],
            _fmt_tokens(r["prompt_tokens"]),
            _fmt_tokens(r["completion_tokens"]),
        ]
        print("".join(_pad(v, w) for v, (_, w) in zip(values, cols)))
    print("-" * sum(width for _, width in cols))
    print(
        f"合计 {total_req} 次请求，输入 {_fmt_tokens(total_pt)}，输出 {_fmt_tokens(total_ct)}"
    )
    return 0


def cmd_cleanup(store: Store, args: argparse.Namespace) -> int:
    result = store.cleanup_logs(keep_days=args.keep_days)
    rate = store.purge_rate_events(max_age=3600.0)
    print(f"已清理超过 {args.keep_days} 天的记录：")
    print(f"  用量明细   {result['usage_log']} 条")
    print(f"  审计日志   {result['audit_log']} 条")
    print(f"  限流事件   {rate} 条（超过 1 小时的）")
    return 0


def cmd_audit(store: Store, args: argparse.Namespace) -> int:
    if args.summary:
        rows = store.audit_summary(days=args.days)
        if not rows:
            print(f"最近 {args.days} 天没有审计记录。")
            return 0
        cols = [("结果", 24), ("次数", 10), ("最近一次", 22)]
        print("".join(_pad(name, width) for name, width in cols))
        print("-" * sum(width for _, width in cols))
        for r in rows:
            values = [r["outcome"], r["n"], r["last_seen"]]
            print("".join(_pad(v, w) for v, (_, w) in zip(values, cols)))
        return 0

    rows = store.audit_query(
        days=args.days,
        limit=args.limit,
        outcome=args.outcome,
        key_id=args.key_id,
        ip=args.ip,
    )
    if not rows:
        print("没有匹配的审计记录。")
        return 0
    cols = [
        ("时间", 21), ("来源 IP", 17), ("密钥", 15),
        ("方法", 7), ("端点", 22), ("状态", 7), ("结果", 22), ("耗时", 9),
    ]
    print("".join(_pad(name, width) for name, width in cols))
    print("-" * sum(width for _, width in cols))
    for r in rows:
        values = [
            r["ts"],
            r["ip"] or "-",
            r["key_prefix"] or "-",
            r["method"] or "-",
            "/v1/" + (r["path"] or ""),
            r["status"],
            r["outcome"],
            f"{r['latency_ms']}ms",
        ]
        print("".join(_pad(v, w) for v, (_, w) in zip(values, cols)))
    print(f"\n共 {len(rows)} 条（最多显示 {args.limit} 条，用 --limit 调整）。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SGLang 网关 API Key 管理工具")
    parser.add_argument(
        "--db",
        default=os.getenv("GATEWAY_DB", "gateway.db"),
        help="SQLite 数据库路径（默认读环境变量 GATEWAY_DB）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="创建一个新的 API Key")
    p_create.add_argument("--name", required=True, help="使用者名称，便于识别")
    p_create.add_argument("--rpm", type=int, default=60, help="每分钟请求上限（默认 60）")
    p_create.add_argument("--daily-tokens", type=int, default=2_000_000, help="每日 token 配额（默认 200 万）")
    p_create.add_argument("--max-concurrent", type=int, default=2, help="单 Key 并发上限（默认 2，全局上限 4）")
    p_create.add_argument("--expires", default=None, help="有效期，如 30d / 12h / 2026-12-31")
    p_create.add_argument("--note", default=None, help="备注")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="列出所有 API Key")
    p_list.add_argument("--all", action="store_true", help="包含已吊销的密钥")
    p_list.set_defaults(func=cmd_list)

    p_revoke = sub.add_parser("revoke", help="吊销 API Key")
    p_revoke.add_argument("identifier", help="密钥编号或前缀")
    p_revoke.set_defaults(func=cmd_revoke)

    p_usage = sub.add_parser("usage", help="查看用量统计")
    p_usage.add_argument("--days", type=int, default=7, help="统计最近多少天")
    p_usage.set_defaults(func=cmd_usage)

    p_audit = sub.add_parser("audit", help="查询请求审计日志")
    p_audit.add_argument("--days", type=int, default=1, help="查询最近多少天")
    p_audit.add_argument("--limit", type=int, default=50, help="最多显示多少条")
    p_audit.add_argument("--outcome", default=None, help="按结果过滤，如 auth_invalid")
    p_audit.add_argument("--key-id", type=int, default=None, help="按密钥编号过滤")
    p_audit.add_argument("--ip", default=None, help="按来源 IP 过滤")
    p_audit.add_argument("--summary", action="store_true", help="只看按结果的汇总统计")
    p_audit.set_defaults(func=cmd_audit)

    p_clean = sub.add_parser("cleanup", help="清理历史日志与限流事件")
    p_clean.add_argument("--keep-days", type=int, default=30, help="保留多少天的明细")
    p_clean.set_defaults(func=cmd_cleanup)

    args = parser.parse_args()
    store = Store(args.db)
    try:
        return args.func(store, args)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
