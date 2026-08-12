"""双解读模式（简单/逐行）与解读范围（targets）端到端测试。

场景：
1. mode=simple 全量解读：各段有内容且事件带 mode=simple；
2. targets=[某函数段, mode=detailed] 只解读该段：只收到该段事件，
   文本呈逐行样式（含「第」「行」），且与 simple 版本不同（两种缓存并存）；
3. 再次请求同一 target（force=none）：cached=true 秒回。

每次运行往代码里嵌入时间戳 nonce，保证段内容哈希变化、不受历史缓存干扰。
"""
import json
import shutil
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8710/api"
PROJ = Path(__file__).parent / "tmp_test_modes"

NONCE = time.strftime("%Y%m%d%H%M%S")

CODE = f'''"""迷你计算器模块（测试运行 {NONCE}）。"""
import math


def circle_area(radius: float) -> float:
    """圆面积，半径为负时抛 ValueError。（{NONCE}）"""
    if radius < 0:
        raise ValueError("半径不能为负")
    return math.pi * radius * radius


def running_mean(values: list) -> list:
    """返回累积平均值序列。（{NONCE}）"""
    out = []
    total = 0.0
    for i, v in enumerate(values, 1):
        total += v
        out.append(total / i)
    return out
'''


def stream_explain(body):
    """POST /explain 并解析 SSE，返回 (meta, 事件列表)。"""
    meta = None
    events = []
    with httpx.stream("POST", BASE + "/explain", json=body,
                      timeout=httpx.Timeout(600, connect=10)) as resp:
        assert resp.status_code == 200, f"HTTP {resp.status_code}"
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:])
                if event == "meta":
                    meta = data
                elif event in ("segment_start", "segment_done", "overview_done"):
                    events.append((event, data))
                elif event == "error":
                    print("[error]", data["message"])
                    sys.exit(1)
    return meta, events


def main() -> None:
    PROJ.mkdir(exist_ok=True)
    fp = PROJ / "calc.py"
    fp.write_text(CODE, encoding="utf-8")

    # 等待模型就绪
    for _ in range(120):
        try:
            r = httpx.get(BASE + "/health", timeout=5.0).json()
            if r["llama"]["ready"]:
                print("[ready]", r["model"])
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        print("[error] 模型未就绪")
        sys.exit(1)

    base_body = {"path": str(fp), "project_root": str(PROJ)}

    # ---- 场景 1：mode=simple 全量解读 ----
    t0 = time.time()
    meta, events = stream_explain({**base_body, "force": "all", "mode": "simple"})
    seg_ids = [s["id"] for s in meta["segments"]]
    done = {d["id"]: d for e, d in events if e == "segment_done"}
    assert set(done) == set(seg_ids), f"缺段: {set(seg_ids) - set(done)}"
    assert all(d.get("mode") == "simple" for d in done.values()), "事件应带 mode=simple"
    assert all(d["text"].strip() for d in done.values()), "各段都应有内容"
    print(f"[场景1 OK] simple 全量：{len(done)} 段全部生成，事件均带 mode=simple，"
          f"用时 {time.time() - t0:.1f}s")

    target = next(s for s in meta["segments"] if s["kind"] == "function")
    simple_text = done[target["id"]]["text"]
    print(f"  target 段：{target['id']}「{target['title']}」")
    print("  simple 版（前 200 字）:", simple_text[:200].replace("\n", " "))

    # ---- 场景 2：targets 只解读该段，模式 detailed ----
    t0 = time.time()
    _, events2 = stream_explain({**base_body, "force": "none", "mode": "simple",
                                 "targets": [{"id": target["id"], "mode": "detailed"}]})
    seg_events = [(e, d) for e, d in events2 if e.startswith("segment_")]
    ids_seen = {d["id"] for _, d in seg_events}
    assert ids_seen == {target["id"]}, f"只应收到目标段事件，实际: {ids_seen}"
    ov2 = [d for e, d in events2 if e == "overview_done"]
    assert ov2 and ov2[0]["cached"] is True, "总览应命中场景1的缓存"
    d2 = next(d for e, d in seg_events if e == "segment_done")
    assert d2.get("mode") == "detailed", "事件应带 mode=detailed"
    assert d2["cached"] is False, "本次应为新生成"
    assert ("第" in d2["text"]) and ("行" in d2["text"]), "逐行样式应含「第 X 行」"
    assert d2["text"] != simple_text, "detailed 与 simple 文本应不同（两种缓存并存）"
    print(f"[场景2 OK] targets 单段 detailed：只收到该段事件、含逐行样式、"
          f"与 simple 版不同，用时 {time.time() - t0:.1f}s")
    print("  detailed 版（前 400 字）:")
    print("  " + d2["text"][:400].replace("\n", "\n  "))

    # ---- 场景 3：同一 target 再请求（force=none）→ 缓存秒回 ----
    t0 = time.time()
    _, events3 = stream_explain({**base_body, "force": "none",
                                 "targets": [{"id": target["id"], "mode": "detailed"}]})
    d3 = next(d for e, d in events3
              if e == "segment_done" and d["id"] == target["id"])
    dt = time.time() - t0
    assert d3["cached"] is True, "应命中缓存"
    assert d3.get("mode") == "detailed"
    assert d3["text"] == d2["text"], "缓存内容应与上次一致"
    assert dt < 10, f"缓存命中应秒回，实际 {dt:.1f}s"
    print(f"[场景3 OK] 同一 target 再请求：cached=true 秒回（{dt:.2f}s）")

    print("\n全部场景通过 ✓")
    shutil.rmtree(PROJ, ignore_errors=True)


if __name__ == "__main__":
    main()
