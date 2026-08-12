"""模型切换测试：切换 -> 等就绪 -> 校验 health 与 config 一致。"""
import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8710/api"


def wait_ready(expect_model: str, timeout: float = 240.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = httpx.get(BASE + "/health", timeout=5.0).json()
            state = f"{r['model']} ready={r['llama']['ready']} phase={r['llama']['phase']}"
            if state != last:
                print("[health]", state)
                last = state
            if r["llama"]["ready"] and r["model"] == expect_model:
                return
        except Exception as e:
            print("[wait]", type(e).__name__)
        time.sleep(3)
    print("[error] 等待超时")
    sys.exit(1)


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5-coder-7b-instruct-q4_k_m.gguf"
    r = httpx.get(BASE + "/models", timeout=10.0).json()
    print("[models]", json.dumps(r, ensure_ascii=False))
    resp = httpx.post(BASE + "/models/switch", json={"name": target}, timeout=30.0)
    print("[switch]", resp.status_code, resp.json())
    assert resp.status_code == 200
    wait_ready(target)
    cfg = json.loads((Path(__file__).parent.parent / "config.json").read_text("utf-8"))
    assert Path(cfg["llama"]["model"]).name == target, cfg["llama"]["model"]
    print("[ok] 已切换至", target, "，config.json 已持久化")


if __name__ == "__main__":
    main()
