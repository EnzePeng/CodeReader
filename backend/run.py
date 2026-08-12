"""CodeReader 启动入口（开发运行与 PyInstaller 打包共用）。"""
import argparse
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn

from app.config import get_config
from app.main import app


def main() -> None:
    cfg = get_config()
    parser = argparse.ArgumentParser(description="CodeReader 本地代码解读工具")
    parser.add_argument("--host", default=cfg["app_host"])
    parser.add_argument("--port", type=int, default=cfg["app_port"])
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"

    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    in_use = probe.connect_ex(("127.0.0.1", args.port)) == 0
    probe.close()
    if in_use:
        print(f"端口 {args.port} 已被占用：可能已有一个 CodeReader 在运行。")
        print(f"请直接访问 {url}，或先关闭旧实例再启动。")
        if not args.no_browser:
            webbrowser.open(url)
        sys.exit(1)

    print(f"CodeReader 正在启动：{url}")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=[url]).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
