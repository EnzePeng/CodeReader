"""跨文件上下文端到端测试。

构造一个双文件小项目：lib.py 定义 TemperatureController，
main.py 调用它。解读 main.py 时应把 lib.py 中类的定义摘要注入
prompt，模型解读里应能指出该类来自 lib.py。
"""
import json
import shutil
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8710/api"
PROJ = Path(__file__).parent / "tmp_test_proj"

LIB = '''"""设备控制库。"""


class TemperatureController:
    """PID 温度控制器，负责读取传感器并调节加热器功率。"""

    def __init__(self, kp: float, ki: float, kd: float):
        self.kp, self.ki, self.kd = kp, ki, kd
        self._integral = 0.0
        self._last_err = 0.0

    def step(self, target: float, current: float, dt: float) -> float:
        err = target - current
        self._integral += err * dt
        deriv = (err - self._last_err) / dt if dt > 0 else 0.0
        self._last_err = err
        return self.kp * err + self.ki * self._integral + self.kd * deriv


def load_profile(path: str) -> list:
    """从文本文件加载升温曲线，每行 "时间,温度"。"""
    out = []
    with open(path) as f:
        for line in f:
            t, temp = line.strip().split(",")
            out.append((float(t), float(temp)))
    return out
'''

MAIN = '''"""烧结炉控温主程序。"""
from lib import TemperatureController, load_profile


def run(profile_path: str):
    ctrl = TemperatureController(kp=2.0, ki=0.1, kd=0.5)
    profile = load_profile(profile_path)
    current = 25.0
    for t, target in profile:
        power = ctrl.step(target, current, dt=1.0)
        current += power * 0.01
    return current
'''


def main() -> None:
    PROJ.mkdir(exist_ok=True)
    (PROJ / "lib.py").write_text(LIB, encoding="utf-8")
    (PROJ / "main.py").write_text(MAIN, encoding="utf-8")

    # 等待就绪
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

    texts = {}
    t0 = time.time()
    with httpx.stream("POST", BASE + "/explain",
                      json={"path": str(PROJ / "main.py"), "force": "all",
                            "project_root": str(PROJ)},
                      timeout=httpx.Timeout(600, connect=10)) as resp:
        assert resp.status_code == 200, resp.status_code
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:])
                if event == "segment_done":
                    texts[data["id"]] = data["text"]
                elif event == "error":
                    print("[error]", data["message"])
                    sys.exit(1)
    print(f"[done] {len(texts)} 段, 用时 {time.time()-t0:.1f}s")
    all_text = "\n\n".join(texts.values())
    print("=" * 60)
    print(all_text)
    print("=" * 60)
    hit = ("lib.py" in all_text) or ("lib 模块" in all_text) or ("lib 文件" in all_text)
    print("跨文件提及 lib.py:", "是" if hit else "否（需人工检查上文）")
    shutil.rmtree(PROJ, ignore_errors=True)


if __name__ == "__main__":
    main()
