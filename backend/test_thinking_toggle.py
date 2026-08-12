"""思考模式开关 API 测试。"""
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8710/api"


def main() -> None:
    h = httpx.get(BASE + "/health", timeout=10).json()
    print("[health]", json.dumps(h.get("thinking"), ensure_ascii=False))
    if not h.get("thinking", {}).get("supported"):
        print("[skip] 当前模型不支持思考模式")
        return

    orig = h["thinking"]["enabled"]
    target = not orig
    r = httpx.post(BASE + "/thinking", json={"enabled": target}, timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    print("[toggle]", body)
    assert body["enabled"] == target

    h2 = httpx.get(BASE + "/health", timeout=10).json()
    assert h2["thinking"]["enabled"] == target, h2["thinking"]

    # 恢复原始状态
    httpx.post(BASE + "/thinking", json={"enabled": orig}, timeout=10)
    print("[restore] thinking=", orig)
    print("OK")


if __name__ == "__main__":
    main()
