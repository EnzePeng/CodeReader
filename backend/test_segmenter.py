"""分段器冒烟测试：对本项目真实文件跑分段，检查覆盖完整性。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app.segmenter import segment_file  # noqa: E402


def check(path: str) -> None:
    src = Path(path).read_text(encoding="utf-8")
    r = segment_file(src, Path(path).suffix)
    print(f"\n=== {path} | strategy={r['strategy']} lines={r['total_lines']} "
          f"segments={len(r['segments'])} outline={len(r['outline'])} ===")
    covered = set()
    for s in r["segments"]:
        print(" {:>4} {:<14} {:>5}-{:<5} {}".format(
            s["id"], s["kind"], s["start_line"], s["end_line"], s["title"]))
        for ln in range(s["start_line"], s["end_line"] + 1):
            assert ln not in covered, f"行 {ln} 被重复覆盖！({s['id']})"
            covered.add(ln)
    missing = set(range(1, r["total_lines"] + 1)) - covered
    assert not missing, f"未覆盖的行: {sorted(missing)[:20]}"
    print("  行覆盖完整，无重叠 K")


if __name__ == "__main__":
    for f in ["app/api.py", "app/segmenter.py", "app/config.py", "run.py"]:
        check(f)
    # 高版本语法（match）应触发缩进回退
    modern = '''"""modern syntax test"""
import os

def handle(cmd):
    match cmd:
        case "a":
            return 1
        case _:
            return 0

class Foo:
    def bar(self):
        pass

if __name__ == "__main__":
    handle("a")
'''
    r = segment_file(modern, ".py")
    print(f"\n=== match 语法 fallback: strategy={r['strategy']} "
          f"segments={[s['title'] for s in r['segments']]}")
    assert r["strategy"] == "indent", "应回退到缩进分段"
    print("  缩进回退正常 K")
    print("\n全部通过")
