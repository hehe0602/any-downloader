"""夸克分享直链修复的离线回归测试（不碰外网）。

跑法：
    py -3.13 tests\\test_quark_fix.py

覆盖的三个回归点：
  1. API 请求固定用 PC 客户端 UA —— 用会话里的浏览器 UA 会被夸克当访客，
     大文件签发直接 23018（2026-11-07 真机 A/B 确认）；
  2. 签发前刷新 ``__puus``（短时效票据），并把刷新后的 Cookie 写回 ``ctx.cookie``，
     Manager 建下载任务时才会用它（不然 CDN 报 412 / 403）；
  3. 「转存兜底」已彻底删除：解析器里不应再有 save / task / capacity 这套东西。
"""
from __future__ import annotations

import asyncio
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.cookie_refresh import merge_set_cookie, refresh_cookie  # noqa: E402
from app.parsers import quark as quark_mod  # noqa: E402
from app.parsers.base import ParseContext, ShareFile, ShareResult  # noqa: E402
from app.parsers.quark import API_UA, QuarkParser  # noqa: E402

EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
)

_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((bool(ok), label))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def test_merge_set_cookie() -> None:
    print("merge_set_cookie")
    cookie = "__pus=abc; __puus=old; __kp=x"
    merged = merge_set_cookie(
        cookie,
        ["__puus=new; Max-Age=86400; Domain=quark.cn; Path=/", "__puus=new; Path=/"],
        "__puus",
    )
    check("__puus=new" in merged, "新票据被写入", merged)
    check("__puus=old" not in merged, "旧票据被覆盖")
    check("__pus=abc" in merged and "__kp=x" in merged, "其它 Cookie 原样保留")
    check(
        merge_set_cookie("__puus=old", [], "__puus") == "__puus=old",
        "没有 Set-Cookie 时原样返回",
    )
    check(
        merge_set_cookie("a=1", ["b=2; Path=/"], "__puus") == "a=1",
        "Set-Cookie 里没有目标名时原样返回",
    )


def test_refresh_guards() -> None:
    print("refresh_cookie 的短路条件（不发网络请求）")
    check(refresh_cookie("baidu", "BDUSS=x") == "BDUSS=x", "站点没配置刷新时原样返回")
    check(
        refresh_cookie("quark", "__kps=fp; __kp=fp") == "__kps=fp; __kp=fp",
        "没有 __pus（访客态）时不刷新",
    )
    check(refresh_cookie("quark", "") == "", "空 Cookie 不刷新")


def test_client_uses_api_ua() -> None:
    print("API 请求 UA")
    parser = QuarkParser()
    ctx = ParseContext(site="quark", cookie="__pus=a; __puus=b", user_agent=EDGE_UA)

    async def run() -> dict[str, str]:
        client = parser._client(ctx, "https://pan.quark.cn/s/abc")
        try:
            return dict(client.headers)
        finally:
            await client.aclose()

    headers = asyncio.run(run())
    check(headers.get("user-agent") == API_UA, "固定使用 PC 客户端 UA", headers.get("user-agent", ""))
    check(headers.get("user-agent") != EDGE_UA, "不再使用会话里的浏览器 UA")


def test_no_transfer_left() -> None:
    print("转存兜底已删除")
    for name in (
        "_save_then_sign",
        "_ensure_save_dir",
        "_wait_save_task",
        "_raise_save_error",
        "_capacity_text",
    ):
        check(not hasattr(QuarkParser, name), f"没有 {name}()")
    source = open(quark_mod.__file__, encoding="utf-8").read()
    check("sharepage/save" not in source, "源码里没有转存接口调用")
    check("/1/clouddrive/task" not in source, "源码里没有转存任务轮询")
    check("fid_list" not in source, "源码里没有转存请求体字段")
    check("capacity limit" not in source, "源码里没有「转存容量不足」的错误分支")


def test_issue_refreshes_cookie() -> None:
    print("签发前刷新票据并写回 ctx")
    parser = QuarkParser()
    ctx = ParseContext(site="quark", cookie="__pus=a; __puus=old", user_agent=EDGE_UA)
    result = ShareResult(site="quark", share_url="https://pan.quark.cn/s/abc", referer="https://pan.quark.cn/s/abc")
    files = [ShareFile(key="f1", name="a.bin", size=1)]

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_refresh(site: str, cookie: str) -> str:
        return cookie.replace("__puus=old", "__puus=new")

    async def fake_sign(client, fids, fids_token=None, result=None):
        return {
            "code": 0,
            "data": [{"fid": fids[0], "download_url": "https://cdn.example/x", "file_name": "a.bin", "size": 1}],
        }

    original_refresh = quark_mod.arefresh_cookie
    quark_mod.arefresh_cookie = fake_refresh
    parser._client = lambda ctx, referer: FakeClient()  # type: ignore[method-assign]
    parser._sign = fake_sign  # type: ignore[method-assign]
    try:
        resolved = asyncio.run(parser.issue_direct_urls(result, files, ctx))
    finally:
        quark_mod.arefresh_cookie = original_refresh

    check(ctx.cookie == "__pus=a; __puus=new", "ctx.cookie 已换成刷新后的票据", ctx.cookie)
    check(len(resolved) == 1 and resolved[0].url.endswith("/x"), "仍能正常拿回直链")


def test_23018_message() -> None:
    print("23018 文案")
    parser = QuarkParser()
    try:
        parser._raise_for(
            {"code": 23018, "message": "download file size limit[abc]"}, "签发下载直链"
        )
    except Exception as e:  # noqa: BLE001
        text = str(e)
        check("访客" in text, "说明是「被当成访客」", text)
        check("转存" not in text, "不再引导转存")
    else:
        check(False, "23018 应当抛错")


def main() -> int:
    test_merge_set_cookie()
    test_refresh_guards()
    test_client_uses_api_ua()
    test_no_transfer_left()
    test_issue_refreshes_cookie()
    test_23018_message()

    failed = [label for ok, label in _results if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(_results)}: {failed}")
        return 1
    print(f"OK {len(_results)} 项断言全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
