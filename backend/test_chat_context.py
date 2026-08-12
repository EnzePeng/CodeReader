"""追问上下文端到端测试：同文件 / 跨文件 / 类方法。"""
import json
import shutil
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8710/api"
PROJ = Path(__file__).parent / "tmp_chat_ctx_proj"
PROJ.mkdir(exist_ok=True)

(PROJ / "a.py").write_text(
    '"""工具库。"""\n\n'
    "def normalize_scores(raw):\n"
    '    """把成绩列表按最大值缩放到 0~1 区间。"""\n'
    "    peak = max(raw) if raw else 1.0\n"
    "    if peak <= 0:\n"
    "        return [0.0 for _ in raw]\n"
    "    return [x / peak for x in raw]\n\n"
    "class MatrixSolver:\n"
    '    """极简矩阵求解器。"""\n\n'
    "    def decompose(self, scores):\n"
    '        """把得分向量对半拆分后逐项相加。"""\n'
    "        half = len(scores) // 2\n"
    "        return [l + r for l, r in zip(scores[:half], scores[half:])]\n",
    encoding="utf-8")
(PROJ / "main.py").write_text(
    '"""主流程。"""\n'
    "from a import MatrixSolver, normalize_scores\n\n"
    "def run_pipeline(raw):\n"
    "    scores = normalize_scores(raw)\n"
    "    solver = MatrixSolver(len(scores))\n"
    "    basis = solver.decompose(scores)\n"
    "    return __LocalSolve(basis)\n\n"
    "def __LocalSolve(basis):\n"
    '    """对基向量做位置加权求和并取平均。"""\n'
    "    total = 0.0\n"
    "    for i, b in enumerate(basis):\n"
    "        total += (i + 1) * b\n"
    "    return total / max(len(basis), 1)\n",
    encoding="utf-8")


def wait_ready(timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(BASE + "/health", timeout=5.0).json()
            if r["llama"]["ready"]:
                return
        except Exception:
            pass
        time.sleep(2)
    print("[error] 模型未就绪")
    sys.exit(1)


def ask(main_path: str, line: int, question: str) -> str:
    parts: list[str] = []
    with httpx.stream("POST", BASE + "/chat",
                      json={
                          "path": main_path,
                          "question": question,
                          "selection": {"start_line": line, "end_line": line},
                          "history": [],
                          "project_root": str(PROJ),
                      },
                      timeout=httpx.Timeout(300, connect=10)) as resp:
        assert resp.status_code == 200, resp.status_code
        event = ""
        for ln in resp.iter_lines():
            if ln.startswith("event:"):
                event = ln[6:].strip()
            elif ln.startswith("data:"):
                data = json.loads(ln[5:])
                if event == "delta":
                    parts.append(data["text"])
                elif event == "error":
                    print("[error]", data["message"])
                    sys.exit(1)
    return "".join(parts)


def main() -> None:
    PROJ.mkdir(exist_ok=True)
    wait_ready()
    main_py = str(PROJ / "main.py")

    # 场景 1：同文件私有函数（模拟用户 TFAITest 场景：选中调用行）
    ans1 = ask(main_py, 8, "这个函数是在哪个文件实现的？这个函数里面做了什么？")
    print("[场景1 同文件 __LocalSolve]")
    print(ans1[:600])
    assert "main.py" in ans1, ans1
    assert "total" in ans1 or "加权" in ans1 or "enumerate" in ans1, ans1
    bad1 = ("没有在提供的代码上下文" in ans1 or "需要查看其他文件" in ans1
            or "无法确定该函数所在" in ans1 or "逻辑缺失" in ans1)
    assert not bad1, "模型仍声称缺少上下文"

    # 场景 2：跨文件函数（选中 normalize_scores 调用行）
    ans2 = ask(main_py, 5, "normalize_scores 在哪个文件定义？里面做了什么？")
    print("\n[场景2 跨文件 normalize_scores]")
    print(ans2[:600])
    assert "a.py" in ans2, ans2

    # 场景 3：类方法（选中 decompose 调用行）
    ans3 = ask(main_py, 7, "decompose 方法在哪个文件的哪个类里？它做了什么？")
    print("\n[场景3 类方法 decompose]")
    print(ans3[:600])
    assert "MatrixSolver" in ans3 and ("a.py" in ans3 or "decompose" in ans3), ans3

    shutil.rmtree(PROJ, ignore_errors=True)
    print("\n全部场景通过 ✓")


if __name__ == "__main__":
    main()
