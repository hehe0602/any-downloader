"""百度网盘分享解析 / 直链签发的离线回归测试（不碰外网）。

跑法：
    py -3.13 tests\\test_baidu.py

覆盖 2026-11 真机确认过的关键点：
  1. 列表必须走 ``/share/wxlist``（老的 ``/share/list`` 对真实分享已回 errno=2）；
  2. 文件夹 key 用绝对路径，子目录下钻才会带 ``dir=/路径`` 而不是 fs_id；
  3. 提取码缺失/错误由 wxlist 的 ``errtype=mispwd-9`` 判定；
  4. 直链签发必须登录，``extra`` 里要带 wxlist 下发的 seckey；
  5. dlink 的 UA 必须是 ``pan.baidu.com``，通过 ``ResolvedFile.user_agent`` 传给引擎；
  6. 112（签名过期）刷新签名、118（sekey 失效）重新列目录，各重试一次。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from urllib.parse import parse_qs, urlsplit

import httpx

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.parsers.baidu import BaiduParser  # noqa: E402
from app.parsers.base import (  # noqa: E402
    LoginRequiredError,
    ParseContext,
    PasscodeRequiredError,
    PasscodeWrongError,
    ShareFile,
    ShareResult,
    ShareUnavailableError,
)

_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((bool(ok), label))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def _json(data: dict, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)


def _form(request: httpx.Request) -> dict[str, str]:
    text = request.content.decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(text).items()}


def _query(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


def _client_for(parser: BaiduParser, handler, cookie: str = "") -> None:
    """把 parser._client 换成走 MockTransport 的 AsyncClient。"""

    def fake_client(ctx: ParseContext, referer: str) -> httpx.AsyncClient:
        headers = {"User-Agent": ctx.user_agent or "test", "Referer": referer}
        if cookie or ctx.cookie:
            headers["Cookie"] = ctx.cookie or cookie
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            headers=headers,
        )

    parser._client = fake_client  # type: ignore[method-assign]


def test_token_and_stoken() -> None:
    print("链接 / stoken 口径")
    parser = BaiduParser()
    check(
        parser._extract_token("https://pan.baidu.com/s/1abcDEF?pwd=1234") == "1abcDEF",
        "标准短链取到含开头 1 的 token",
    )
    check(
        parser._extract_surl("https://pan.baidu.com/s/1abcDEF") == "abcDEF",
        "verify 口径去掉开头的 1",
    )
    check(
        parser._extract_token("https://pan.baidu.com/share/init?surl=xyz") == "1xyz",
        "旧式 init 链接补回开头的 1",
    )
    check(parser._as_token("oldToken") == "1oldToken", "历史 pwd_id 自动补 1")
    check(
        parser._decode_seckey("ab-cd~ef_ghi") == "ab+cd=ef/ghi",
        "URL-safe Base64 → 标准 Base64",
    )
    stoken = parser._pack_stoken("42", "99", "ab+cd=")
    check(stoken == "42|99|ab+cd=", "stoken 打包", stoken)
    check(parser._unpack_stoken(stoken) == ("42", "99", "ab+cd="), "stoken 解包")
    check(
        parser._unpack_stoken("42|99") == ("42", "99", ""),
        "旧 stoken（没有 seckey）也能解",
    )


def test_parse_share_and_dir() -> None:
    print("wxlist 列目录 / 文件夹下钻")
    forms: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/share/wxlist"
        form = _form(request)
        forms.append(form)
        if form.get("root") == "1":
            return _json(
                {
                    "errno": 0,
                    "data": {
                        "shareid": 42,
                        "uk": 99,
                        "seckey": "ab-cd~",
                        "title": "/测试文件夹",
                        "has_more": False,
                        "list": [
                            {
                                "fs_id": 100,
                                "server_filename": "测试文件夹",
                                "isdir": "1",
                                "size": "0",
                                "path": "/测试文件夹",
                            }
                        ],
                    },
                },
                headers={"Set-Cookie": "BDCLND=bdclnd-value; Path=/"},
            )
        return _json(
            {
                "errno": 0,
                "data": {
                    "shareid": 42,
                    "uk": 99,
                    "seckey": "ab-cd~",
                    "has_more": False,
                    "list": [
                        {
                            "fs_id": 7,
                            "server_filename": "a.bin",
                            "isdir": 0,
                            "size": "123",
                            "path": "/测试文件夹/a.bin",
                            "md5": "m",
                        }
                    ],
                },
            }
        )

    parser = BaiduParser()
    _client_for(parser, handler)
    ctx = ParseContext(site="baidu")
    url = "https://pan.baidu.com/s/1tokenXYZ?pwd=1234"

    async def run() -> tuple[ShareResult, list[ShareFile]]:
        result = await parser.parse_share(url, ctx)
        children = await parser.list_dir(result, result.files[0].key, ctx)
        return result, children

    result, children = asyncio.run(run())
    check(result.stoken == "42|99|ab+cd=", "stoken 里带 seckey", result.stoken)
    check(result.share_title == "测试文件夹", "标题去掉路径前缀", result.share_title)
    check(result.files[0].key == "/测试文件夹", "文件夹 key 用绝对路径")
    check(result.files[0].is_dir, "文件夹标记正确")
    check(forms[0].get("shorturl") == "1tokenXYZ", "wxlist shorturl 含开头 1")
    check(forms[0].get("pwd") == "1234", "提取码传给 wxlist")
    check(forms[0].get("root") == "1", "根目录 root=1")
    check(forms[1].get("dir") == "/测试文件夹", "子目录 dir 是路径", forms[1].get("dir", ""))
    check(forms[1].get("root") == "0", "子目录 root=0")
    check(len(children) == 1 and children[0].key == "7", "文件 key 用 fs_id")
    check(children[0].extra.get("fs_id") == "7", "extra 带 fs_id")
    check(children[0].extra.get("seckey") == "ab+cd=", "子条目也带 seckey")


def test_passcode_mapping() -> None:
    print("提取码错误映射")
    parser = BaiduParser()

    def handler(request: httpx.Request) -> httpx.Response:
        return _json({"errno": -130, "errtype": "mispwd-9"})

    _client_for(parser, handler)
    ctx = ParseContext(site="baidu")

    async def run(url: str) -> str:
        try:
            await parser.parse_share(url, ctx)
        except PasscodeRequiredError as e:
            return f"required:{e}"
        except PasscodeWrongError as e:
            return f"wrong:{e}"
        except Exception as e:  # noqa: BLE001
            return f"other:{type(e).__name__}:{e}"
        return "ok"

    missing = asyncio.run(run("https://pan.baidu.com/s/1abc"))
    wrong = asyncio.run(run("https://pan.baidu.com/s/1abc?pwd=zzzz"))
    check(missing.startswith("required:"), "没带提取码 → passcode_required", missing)
    check(wrong.startswith("wrong:"), "带错提取码 → passcode_wrong", wrong)

    def canceled(request: httpx.Request) -> httpx.Response:
        return _json({"errno": -21, "show_msg": "该分享已被取消"})

    _client_for(parser, canceled)
    try:
        asyncio.run(parser.parse_share("https://pan.baidu.com/s/1abc?pwd=1", ctx))
    except ShareUnavailableError as e:
        check("取消" in str(e), "取消分享 → unavailable", str(e))
    else:
        check(False, "取消分享应当抛错")


def _issue_handler(
    *,
    download_errors: list[dict] | None = None,
    seckey_after_refresh: str = "ab-cd~",
):
    """构造签名流程的 Mock handler；download_errors 按调用顺序先返回。"""
    state = {"download_calls": 0, "tpl_calls": 0, "wxlist_calls": 0, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/gettemplatevariable":
            return _json(
                {
                    "errno": 0,
                    "result": {"bdstoken": "tok", "loginstate": 1, "username": "u"},
                }
            )
        if path == "/share/tplconfig":
            state["tpl_calls"] += 1
            return _json(
                {
                    "errno": 0,
                    "data": {"sign": f"sign{state['tpl_calls']}", "timestamp": 12345},
                }
            )
        if path == "/share/wxlist":
            state["wxlist_calls"] += 1
            return _json(
                {
                    "errno": 0,
                    "data": {
                        "shareid": 42,
                        "uk": 99,
                        # 第一次列目录沿用 stoken 里的 seckey；118 后刷新成新的
                        "seckey": (
                            "ab-cd~"
                            if state["wxlist_calls"] == 1
                            else seckey_after_refresh
                        ),
                        "has_more": False,
                        "list": [],
                    },
                }
            )
        if path == "/api/sharedownload":
            state["requests"].append(request)
            state["download_calls"] += 1
            if download_errors and state["download_calls"] <= len(download_errors):
                return _json(download_errors[state["download_calls"] - 1])
            return _json(
                {
                    "errno": 0,
                    "list": [
                        {
                            "server_filename": "a.bin",
                            "size": "123",
                            "dlink": "https://d.pcs.baidu.com/file/x",
                        }
                    ],
                }
            )
        return _json({"errno": 404}, status=404)

    return handler, state


def _issue_setup(handler):
    parser = BaiduParser()
    _client_for(parser, handler)
    ctx = ParseContext(
        site="baidu", cookie="BDUSS=abc; STOKEN=s; BAIDUID=xx%3D", user_agent=""
    )
    result = ShareResult(
        site="baidu",
        share_url="https://pan.baidu.com/s/1tokenXYZ?pwd=1234",
        pwd_id="1tokenXYZ",
        stoken="42|99|ab+cd=",
        referer="https://pan.baidu.com/s/1tokenXYZ",
    )
    files = [
        ShareFile(key="7", name="a.bin", size=123, extra={"fs_id": "7"}),
    ]
    return parser, ctx, result, files


def test_issue_success() -> None:
    print("登录后签发直链")
    handler, state = _issue_handler()
    parser, ctx, result, files = _issue_setup(handler)
    resolved = asyncio.run(parser.issue_direct_urls(result, files, ctx))
    check(len(resolved) == 1, "返回一个直链")
    got = resolved[0]
    check(got.url == "https://d.pcs.baidu.com/file/x", "直链 URL 正确")
    check(got.user_agent == "pan.baidu.com", "下载 UA 固定为 pan.baidu.com", got.user_agent)
    check(got.filename == "a.bin" and got.size == 123, "文件名 / 大小来自接口")

    req = state["requests"][0]
    query = _query(req)
    form = _form(req)
    check(query.get("sign") == "sign1", "带 sign", query.get("sign", ""))
    check(query.get("timestamp") == "12345", "带 timestamp")
    check(query.get("bdstoken") == "tok", "带 bdstoken")
    check(query.get("clienttype") == "0", "clienttype=0（LinkSwift 口径）")
    check(form.get("primaryid") == "42", "primaryid=shareid")
    check(form.get("uk") == "99", "uk=分享者 uk")
    check(form.get("fid_list") == "[7]", "fid_list 是 JSON 数组")
    check(form.get("product") == "share", "product=share")
    check("type" not in form, "第一套不带 type（LinkSwift 口径）")
    check(
        json.loads(form.get("extra") or "{}").get("sekey") == "ab+cd=",
        "extra.sekey 是标准 Base64", form.get("extra", ""),
    )
    check(
        req.headers.get("User-Agent") == "netdisk;",
        "sharedownload 用客户端 UA",
        req.headers.get("User-Agent", ""),
    )


def test_issue_requires_login() -> None:
    print("未登录签发")
    handler, _ = _issue_handler()
    parser, ctx, result, files = _issue_setup(handler)
    ctx.cookie = "BAIDUID=x"
    try:
        asyncio.run(parser.issue_direct_urls(result, files, ctx))
    except LoginRequiredError as e:
        check("登录" in str(e), "没有 BDUSS → LoginRequiredError", str(e))
    else:
        check(False, "未登录应当被拦下")


def test_issue_error_mapping() -> None:
    print("签发错误映射")
    for errno, label, expect in [
        (9019, "9019 need verify", LoginRequiredError),
        (-20, "-20 风控", ShareUnavailableError),
        (112, "112 签名过期", ShareUnavailableError),
        (-9, "-9 文件不存在", ShareUnavailableError),
        (-1, "-1 违规", ShareUnavailableError),
    ]:
        # 112 先重试一次；连续两次 112 才说明刷新也救不回来
        errors = [{"errno": errno, "errmsg": "x"}]
        if errno == 112:
            errors.append({"errno": 112, "errmsg": "x"})
        handler, state = _issue_handler(download_errors=errors)
        parser, ctx, result, files = _issue_setup(handler)
        try:
            asyncio.run(parser.issue_direct_urls(result, files, ctx))
        except expect as e:
            if errno == 112:
                check(
                    state["tpl_calls"] >= 2,
                    "112 会刷新一次签名",
                    f"tpl_calls={state['tpl_calls']}",
                )
                check("112" in str(e), "112 错误文案", str(e))
            elif errno == 9019:
                check("登录" in str(e), "9019 引导登录", str(e))
            elif errno == -20:
                check("风控" in str(e), "-20 说明风控", str(e))
            else:
                check(True, f"{label} → {expect.__name__}")
        except Exception as e:  # noqa: BLE001
            check(False, f"{label} 异常类型不对：{type(e).__name__}: {e}")
        else:
            check(False, f"{label} 应当抛错")


def test_118_refresh_seckey() -> None:
    print("118 缺 sekey → 重新列目录再试")
    handler, state = _issue_handler(
        download_errors=[{"errno": 118}], seckey_after_refresh="new-sec~"
    )
    parser, ctx, result, files = _issue_setup(handler)
    resolved = asyncio.run(parser.issue_direct_urls(result, files, ctx))
    check(len(resolved) == 1, "刷新 seckey 后签发成功")
    check(
        state["wxlist_calls"] == 2,
        "签发前列一次 + 118 后再列一次",
        f"calls={state['wxlist_calls']}",
    )
    second = state["requests"][-1]
    check(
        json.loads(_form(second).get("extra") or "{}").get("sekey") == "new+sec=",
        "第二次请求用了新 seckey",
        _form(second).get("extra", ""),
    )


def test_dlink_redirect_resolved() -> None:
    print("dlink 302 先解析成最终 CDN 地址")
    base_handler, _ = _issue_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/file/x":
            return httpx.Response(
                302,
                headers={"Location": "https://d.pcs.baidu.com/file/final?token=1"},
            )
        if path == "/file/final":
            return httpx.Response(
                206,
                content=b"xx",
                headers={"Content-Range": "bytes 0-1/123"},
            )
        return base_handler(request)

    parser, ctx, result, files = _issue_setup(handler)
    resolved = asyncio.run(parser.issue_direct_urls(result, files, ctx))
    check(
        resolved[0].url == "https://d.pcs.baidu.com/file/final?token=1",
        "返回最终 CDN 地址而不是原始 dlink",
        resolved[0].url,
    )


def test_variant_fallback() -> None:
    print("参数类错误换 clienttype/UA 再试")
    handler, state = _issue_handler(download_errors=[{"errno": 113}])
    parser, ctx, result, files = _issue_setup(handler)
    resolved = asyncio.run(parser.issue_direct_urls(result, files, ctx))
    check(len(resolved) == 1, "换口径后签发成功")
    check(state["download_calls"] == 2, "只多试了一次", f"calls={state['download_calls']}")
    second = state["requests"][-1]
    check(
        _query(second).get("clienttype") == "12",
        "第二套用 clienttype=12（baiduwp 口径）",
        _query(second).get("clienttype", ""),
    )
    check(
        second.headers.get("User-Agent") != "netdisk;",
        "第二套换成网页 UA",
        second.headers.get("User-Agent", ""),
    )
    check(_form(second).get("type") == "nolimit", "第二套带 type=nolimit")


def main() -> int:
    test_token_and_stoken()
    test_parse_share_and_dir()
    test_passcode_mapping()
    test_issue_success()
    test_issue_requires_login()
    test_issue_error_mapping()
    test_118_refresh_seckey()
    test_dlink_redirect_resolved()
    test_variant_fallback()

    failed = [label for ok, label in _results if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(_results)}: {failed}")
        return 1
    print(f"OK {len(_results)} 项断言全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
