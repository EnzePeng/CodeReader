"""API 端到端测试：等待模型就绪 -> 流式解读 run.py -> 打印事件摘要。"""
import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8710/api"
TARGET = str(Path(__file__).parent / "run.py")


def wait_ready(timeout: float = 360.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = httpx.get(BASE + "/health", timeout=5.0).json()
            phase = r["llama"]["phase"]
            if r["llama"]["ready"]:
                print(f"[ready] 模型 {r['model']} 已就绪")
                return
            if phase == "error":
                print("[error]", r["llama"]["detail"])
                sys.exit(1)
            if phase != last:
                print(f"[wait] {phase}: {r['llama']['detail']}")
                last = phase
        except Exception as e:
            if str(e) != last:
                print("[wait] 后端未就绪:", type(e).__name__)
                last = str(e)
        time.sleep(2)
    print("[error] 等待超时")
    sys.exit(1)


def run_explain() -> None:
    t0 = time.time()
    n_events = 0
    texts = {}
    with httpx.stream("POST", BASE + "/explain",
                      json={"path": TARGET, "force": "none",
                            "project_root": str(Path(__file__).parent)},
                      timeout=httpx.Timeout(600, connect=10)) as resp:
        assert resp.status_code == 200, resp.status_code
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:])
                n_events += 1
                if event == "meta":
                    print(f"[meta] {len(data['segments'])} 段, 语言={data['language']}, "
                          f"策略={data['strategy']}")
                elif event == "overview_done":
                    print(f"[总览][缓存={data['cached']}] {data['text'][:120]}…")
                elif event == "segment_done":
                    texts[data["id"]] = data["text"]
                    print(f"[{data['id']} 完成][缓存={data['cached']}] "
                          f"{data['text'][:80].replace(chr(10), ' ')}…")
                elif event == "error":
                    print("[error]", data["message"])
                    sys.exit(1)
                elif event == "done":
                    print(f"[done] 共 {n_events} 个事件, 用时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    wait_ready()
    run_explain()
