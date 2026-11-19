"""高速多并发下载器 - 入口。

用法：
    py -3.13 main.py                     # GUI 模式（pywebview）
    py -3.13 main.py --cli URL -o 文件   # 命令行模式（可用于无界面/脚本）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 单实例互斥体句柄（必须持有到进程结束，否则互斥体会被释放）
_INSTANCE_MUTEX: dict = {"handle": None}


def _acquire_instance_mutex() -> bool:
    """确保只有一个 GUI 实例在跑（托盘常驻时最容易重复启动）。

    首次调用持有命名互斥体并返回 True；已有同名实例则返回 False。
    非 Windows 或创建失败时一律放行，避免误伤。
    """
    if sys.platform != "win32":
        return True
    if _INSTANCE_MUTEX["handle"] is not None:
        return True
    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, "Local\\HighSpeedDownloader.SingleInstance")
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _INSTANCE_MUTEX["handle"] = handle
        return True
    except Exception:  # noqa: BLE001
        return True


def _warn_already_running() -> None:
    message = (
        "高速下载器已经在运行（可能被最小化到系统托盘）。\n\n"
        "请双击任务栏右下角的托盘图标恢复窗口；\n"
        "如需彻底退出，右键托盘图标选择「退出下载器」。"
    )
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "高速下载器", 0x40)
            return
        except Exception:  # noqa: BLE001
            pass
    print(message)


def run_gui() -> int:
    import threading

    import webview

    from app.api import JsApi
    from app.downloader import ACTIVE_STATUSES
    from app.manager import DownloadManager
    from app.tray import TrayIcon

    manager = DownloadManager()
    manager.install_signal_handlers()
    api = JsApi(manager)

    web_dir = os.path.join(BASE_DIR, "web")
    window = webview.create_window(
        "高速下载器",
        url=os.path.join(web_dir, "index.html"),
        js_api=api,
        width=1060,
        height=780,
        min_size=(880, 620),
        background_color="#eef2ff",
        text_select=True,
    )
    manager.bind_window(window)

    state = {"quitting": False}

    def quit_app() -> None:
        """真正退出：销毁所有窗口 → 事件循环退出 → finally 里保存进度。"""
        state["quitting"] = True
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:  # noqa: BLE001
                pass

    tray = TrayIcon(window, on_exit=quit_app)

    def on_closing() -> bool:
        """点 X：藏到托盘而不是退出（托盘不可用时维持原来的关闭即退出）。"""
        if state["quitting"]:
            return True
        if not tray.attach():
            return True
        # 连登录窗一起藏起来，托盘里双击再恢复
        for w in list(webview.windows):
            try:
                w.hide()
            except Exception:  # noqa: BLE001
                pass
        tray.show_balloon(
            "已最小化到系统托盘",
            "关闭窗口不会中断下载。双击托盘图标恢复界面，右键可退出。",
            once=True,
        )
        return False  # 取消关闭

    def on_closed() -> None:
        # 主窗真的关闭时：收走托盘图标与其余窗口，避免进程/图标残留
        tray.dispose_direct()

        def close_others() -> None:
            for other in list(webview.windows):
                if other is window:
                    continue
                try:
                    other.destroy()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=close_others, daemon=True).start()

    def tooltip_loop() -> None:
        """托盘悬停提示：显示正在下载的任务数与总速度。"""
        while not state["quitting"]:
            time.sleep(3)
            if not tray.available:
                continue
            active = [
                t for t in manager.tasks.values() if t.status in ACTIVE_STATUSES
            ]
            if not active:
                tray.update_tooltip("高速下载器 · 后台运行中")
            else:
                speed = sum(t.speed for t in active)
                tray.update_tooltip(
                    f"高速下载器 · {len(active)} 个任务下载中 · {_fmt_bytes(speed)}/s"
                )

    window.events.closing += on_closing
    window.events.closed += on_closed
    threading.Thread(target=tooltip_loop, name="tray-tooltip", daemon=True).start()

    try:
        # private_mode=False + 固定 storage_path：WebView2 的登录态持久化到项目目录，
        # 扫码登录一次即可长期复用（会话功能见 app/session.py）
        webview.start(
            private_mode=False,
            storage_path=os.path.join(BASE_DIR, "会话数据"),
        )
    finally:
        tray.dispose()
        manager.shutdown()
    return 0


def run_cli(args: argparse.Namespace) -> int:
    from app.manager import DownloadManager

    # persist_tasks=False：CLI 不读也不写 GUI 的任务索引（它不受单实例互斥体约束）
    manager = DownloadManager(persist_tasks=False)
    manager.install_signal_handlers()

    output = os.path.abspath(args.output)
    opts = {
        "url": args.url,
        "save_dir": os.path.dirname(output),
        "filename": os.path.basename(output),
        "concurrency": args.threads,
        "chunk_size": args.chunk_mb,
        # CLI 里 -t 是显式指定，默认不要自适应（否则要从 8 路爬上来，小文件明显变慢）；
        # 想用自适应就加 --adaptive。
        "adaptive_concurrency": bool(getattr(args, "adaptive", False)),
        "referer": args.referer or "",
        "cookie": args.cookie or "",
        "use_http2": args.http2,
    }
    try:
        snap = manager.add_download(opts).result()
    except Exception as e:  # noqa: BLE001
        print(f"[错误] {e}")
        manager.shutdown()
        return 1
    tid = snap["id"]

    print(f"任务已建立：{snap.get('filename') or snap['url']}")
    terminal = {"completed", "canceled", "error"}
    try:
        while True:
            time.sleep(0.5)
            t = manager.snapshot_now(tid)
            if t is None:
                break
            line = (
                f"\r{t['status']:<10} {t['percent']:6.2f}%  "
                f"{_fmt_bytes(t['done_bytes'])}/{_fmt_bytes(t['total_size'])}  "
                f"{_fmt_bytes(t['speed'])}/s  "
                f"块 {t['chunks_done']}/{t['chunk_count']}  "
                f"ETA {_fmt_eta(t['eta'])}        "
            )
            print(line, end="", flush=True)
            if t["status"] in terminal:
                print()
                if t["status"] == "error":
                    print(f"[错误] {t['error']}")
                break
    except KeyboardInterrupt:
        print("\n[中断] 正在保存进度...")
        manager.pause_task(tid).result(timeout=30)
    finally:
        manager.shutdown()

    final = manager.snapshot_now(tid)
    return 0 if final and final["status"] == "completed" else 1


def _fmt_bytes(n: float | None) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_eta(seconds: int | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="高速多并发下载器")
    parser.add_argument("url", nargs="?", help="下载链接（CLI 模式）")
    parser.add_argument("-o", "--output", help="保存文件完整路径（CLI 模式）")
    parser.add_argument("-t", "--threads", type=int, default=64, help="并发数，默认 64")
    parser.add_argument(
        "-c", "--chunk-mb", type=int, default=4, help="分块大小 MB（2~5），默认 4"
    )
    parser.add_argument("--referer", default="", help="自定义 Referer")
    parser.add_argument("--cookie", default="", help="自定义 Cookie")
    parser.add_argument("--http2", action="store_true", help="启用 HTTP/2（默认关闭）")
    parser.add_argument(
        "--adaptive",
        action="store_true",
        help="启用并发自适应（默认关闭：CLI 显式给的 -t 立即生效）",
    )
    parser.add_argument("--cli", action="store_true", help="强制使用命令行模式")
    args = parser.parse_args()

    if args.cli or (args.url and args.output):
        if not args.url or not args.output:
            parser.error("CLI 模式需要同时提供 URL 和 -o 输出路径")
        return run_cli(args)

    # GUI 模式：避免托盘常驻时重复启动（CLI 不限制，便于脚本并行跑）
    if not _acquire_instance_mutex():
        _warn_already_running()
        return 0
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
