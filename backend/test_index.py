"""项目符号索引冒烟测试：以 backend/ 自身为项目根。"""
from pathlib import Path

from app import project_index

root = str(Path(__file__).parent)
idx = project_index.get_index(root)
assert idx is not None
print("files:", idx["files"], "symbols:", len(idx["symbols"]),
      "build_ms:", idx["build_ms"])
assert idx["files"] >= 5
assert "segment_file" in idx["symbols"], sorted(idx["symbols"])[:30]

code = """
def explain():
    seg = segment_file(text, ".py")
    filt = ThinkFilter()
    key = make_key("a", "b")
"""
ctx = project_index.related_context(idx, code, "app/api.py")
print("---- context ----")
print(ctx)
assert "segment_file" in ctx and "segmenter.py" in ctx
assert "ThinkFilter" in ctx
assert "make_key" in ctx
# 本段定义的 explain 不应被注入；且 app/api.py（正斜杠写法）应视同当前文件
assert "api.py" not in ctx, ctx

# 当前文件内定义的符号应被排除
ctx2 = project_index.related_context(idx, "segment_file(x)", "app\\segmenter.py")
assert "segment_file" not in ctx2, ctx2

print("OK")
