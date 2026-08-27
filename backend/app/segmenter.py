"""代码分段器。

三级策略：
1. Python 文件优先用 ast 精准分段（模块说明/导入/全局定义/类/函数/入口）；
2. ast 解析失败（如更高版本语法）时，退回基于缩进的结构分段；
3. 非 Python 文件用通用分块（按空行边界切成适中大小的块）。
"""
import ast
import hashlib
import re
import warnings
from typing import Any, Dict, List, Optional, Tuple, Union

# 超过该行数的类会被拆成 类头 + 各方法
CLASS_SPLIT_LINES = 60
# 超过该行数的函数会按顶层语句边界拆成多个部分
FUNC_SPLIT_LINES = 300
FUNC_PART_LINES = 150
# 通用分块的目标行数
GENERIC_TARGET_LINES = 50
GENERIC_MAX_LINES = 90

LANG_BY_EXT = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".sh": "shell", ".bash": "shell", ".ps1": "powershell", ".bat": "bat", ".cmd": "bat",
    ".sql": "sql", ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "ini", ".ini": "ini",
    ".xml": "xml", ".md": "markdown", ".txt": "plaintext", ".lua": "lua", ".r": "r",
    ".kt": "kotlin", ".swift": "swift", ".vue": "html", ".conf": "ini", ".cfg": "ini",
}

CODE_EXTS = set(LANG_BY_EXT.keys())

# 目录浏览与项目扫描时统一跳过的目录
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".svn", ".hg", ".idea",
             ".vscode", "venv", ".venv", "env", "dist", "build", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", ".tox", "site-packages",
             "$RECYCLE.BIN", "System Volume Information"}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def language_for(ext: str) -> str:
    return LANG_BY_EXT.get(ext.lower(), "plaintext")


def _seg(kind: str, title: str, start: int, end: int) -> Dict[str, Any]:
    return {"kind": kind, "title": title, "start_line": start, "end_line": end}


def _node_start(node: ast.stmt) -> int:
    """含装饰器的起始行。"""
    decorators = getattr(node, "decorator_list", None) or []
    linenos = [getattr(decorator, "lineno", node.lineno) for decorator in decorators]
    linenos.append(node.lineno)
    return min(linenos)


def _split_long_function(
    seg: Dict[str, Any], node: Union[ast.FunctionDef, ast.AsyncFunctionDef]
) -> List[Dict[str, Any]]:
    """超长函数按函数体顶层语句边界拆分。"""
    total = seg["end_line"] - seg["start_line"] + 1
    if total <= FUNC_SPLIT_LINES or not getattr(node, "body", None):
        return [seg]
    parts: List[Dict[str, Any]] = []
    part_start = seg["start_line"]
    part_no = 1
    body = list(node.body)
    for i, stmt in enumerate(body):
        stmt_end = getattr(stmt, "end_lineno", stmt.lineno)
        is_last = i == len(body) - 1
        if is_last:
            stmt_end = seg["end_line"]
        if stmt_end - part_start + 1 >= FUNC_PART_LINES or is_last:
            parts.append(_seg(seg["kind"], f"{seg['title']} · 第{part_no}部分",
                              part_start, stmt_end))
            part_start = stmt_end + 1
            part_no += 1
    if len(parts) <= 1:
        seg_copy = dict(seg)
        return [seg_copy]
    return parts


def _segment_class(node: ast.ClassDef, segments: List[Dict[str, Any]]) -> None:
    start = _node_start(node)
    end = node.end_lineno or node.lineno
    if end - start + 1 <= CLASS_SPLIT_LINES:
        segments.append(_seg("class", f"类 {node.name}", start, end))
        return
    # 大类：拆成 类头(+属性) 与各方法
    groups: List[Dict[str, Any]] = []
    pending_start: Optional[int] = None
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if pending_start is not None:
                groups.append(_seg("class_header", f"类 {node.name}（定义与属性）",
                                   pending_start, _node_start(stmt) - 1))
                pending_start = None
            m_start = _node_start(stmt)
            m_end = stmt.end_lineno or stmt.lineno
            m_seg = _seg("method", f"{node.name}.{stmt.name}()", m_start, m_end)
            groups.extend(_split_long_function(m_seg, stmt))
        else:
            if pending_start is None:
                pending_start = stmt.lineno if not groups else getattr(stmt, "lineno", stmt.lineno)
    if pending_start is not None:
        groups.append(_seg("class_header", f"类 {node.name}（类级代码）", pending_start, end))
    if groups:
        first_start = groups[0]["start_line"]
        if first_start > start:
            # 类定义行（含装饰器、docstring 之前的部分）作为类头段
            if groups[0]["kind"] == "class_header":
                groups[0]["start_line"] = start
            else:
                groups.insert(0, _seg("class_header", f"类 {node.name}（定义）",
                                      start, first_start - 1))
    segments.extend(groups)


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__")


def _outline_from_ast(tree: ast.Module) -> List[Dict[str, Any]]:
    outline: List[Dict[str, Any]] = []

    def visit(
        nodes: List[ast.stmt], dest: List[Dict[str, Any]], prefix: str = ""
    ) -> None:
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                item: Dict[str, Any] = {
                    "kind": "function" if not prefix else "method",
                    "name": f"{n.name}()",
                    "start_line": _node_start(n),
                    "end_line": n.end_lineno or n.lineno,
                    "children": [],
                }
                dest.append(item)
                visit(n.body, item["children"], prefix + n.name + ".")
            elif isinstance(n, ast.ClassDef):
                item = {
                    "kind": "class",
                    "name": n.name,
                    "start_line": _node_start(n),
                    "end_line": n.end_lineno or n.lineno,
                    "children": [],
                }
                dest.append(item)
                visit(n.body, item["children"], prefix + n.name + ".")

    visit(tree.body, outline)
    return outline


def segment_python_ast(source: str) -> Optional[Dict[str, Any]]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None

    segments: List[Dict[str, Any]] = []
    body = list(tree.body)
    i = 0

    # 模块 docstring
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        module_docstring = body[0]
        segments.append(
            _seg(
                "docstring",
                "模块说明",
                module_docstring.lineno,
                module_docstring.end_lineno or module_docstring.lineno,
            )
        )
        i = 1

    def flush_group(group: List[Tuple[int, int]], kind: str, title: str) -> None:
        if group:
            segments.append(_seg(kind, title, group[0][0], group[-1][1]))
            group.clear()

    import_group: List[Tuple[int, int]] = []
    global_group: List[Tuple[int, int]] = []
    code_group: List[Tuple[int, int]] = []

    def flush_all() -> None:
        flush_group(import_group, "imports", "导入依赖")
        flush_group(global_group, "globals", "全局定义")
        flush_group(code_group, "code", "模块级代码")

    while i < len(body):
        node = body[i]
        start = _node_start(node)
        end = getattr(node, "end_lineno", node.lineno)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            flush_group(global_group, "globals", "全局定义")
            flush_group(code_group, "code", "模块级代码")
            import_group.append((start, end))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            flush_all()
            f_seg = _seg("function", f"函数 {node.name}()", start, end)
            segments.extend(_split_long_function(f_seg, node))
        elif isinstance(node, ast.ClassDef):
            flush_all()
            _segment_class(node, segments)
        elif _is_main_guard(node):
            flush_all()
            segments.append(_seg("main", "程序入口", start, end))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            flush_group(import_group, "imports", "导入依赖")
            flush_group(code_group, "code", "模块级代码")
            global_group.append((start, end))
        else:
            flush_group(import_group, "imports", "导入依赖")
            flush_group(global_group, "globals", "全局定义")
            code_group.append((start, end))
        i += 1
    flush_all()

    segments.sort(key=lambda s: s["start_line"])
    return {"segments": segments, "outline": _outline_from_ast(tree), "strategy": "ast"}


_DEF_RE = re.compile(r"^(async\s+def|def|class)\s+(\w+)")
_TOP_RE = re.compile(r"^(@|async\s+def\s|def\s|class\s)")
_IMPORT_RE = re.compile(r"^(import\s|from\s)")


def segment_python_indent(source: str) -> Dict[str, Any]:
    """基于缩进的 Python 结构分段（ast 失败时的回退，可处理更高版本语法）。"""
    lines = source.splitlines()
    n = len(lines)
    boundaries: List[Dict[str, Any]] = []  # {line, kind, name}
    i = 0
    while i < n:
        raw = lines[i]
        if raw and not raw[0].isspace():
            if _TOP_RE.match(raw):
                # 装饰器归属到后续的 def/class
                j = i
                while j < n and lines[j].lstrip().startswith("@"):
                    j += 1
                m = _DEF_RE.match(lines[j]) if j < n else None
                if m:
                    kind = "class" if m.group(1) == "class" else "function"
                    name = m.group(2)
                    boundaries.append({"line": i + 1, "kind": kind, "name": name})
                    # 跳过该块（直到下一个顶层非空行）
                    j += 1
                    while j < n and (not lines[j] or lines[j][0].isspace()
                                     or lines[j].lstrip() == ""):
                        j += 1
                    i = j
                    continue
            elif _IMPORT_RE.match(raw):
                if not boundaries or boundaries[-1]["kind"] != "imports_open":
                    boundaries.append({"line": i + 1, "kind": "imports_open", "name": ""})
            elif raw.startswith("if __name__"):
                boundaries.append({"line": i + 1, "kind": "main", "name": ""})
        i += 1

    segments: List[Dict[str, Any]] = []
    outline: List[Dict[str, Any]] = []
    if not boundaries:
        return {"segments": _generic_chunks(lines), "outline": [], "strategy": "generic"}

    for idx, b in enumerate(boundaries):
        start = b["line"]
        end = boundaries[idx + 1]["line"] - 1 if idx + 1 < len(boundaries) else n
        if end < start:
            continue
        if b["kind"] == "imports_open":
            segments.append(_seg("imports", "导入依赖", start, end))
        elif b["kind"] == "main":
            segments.append(_seg("main", "程序入口", start, end))
        elif b["kind"] == "class":
            segments.append(_seg("class", f"类 {b['name']}", start, end))
            outline.append({"kind": "class", "name": b["name"], "start_line": start,
                            "end_line": end, "children": []})
        else:
            segments.append(_seg("function", f"函数 {b['name']}()", start, end))
            outline.append({"kind": "function", "name": f"{b['name']}()", "start_line": start,
                            "end_line": end, "children": []})
    if boundaries[0]["line"] > 1:
        segments.insert(0, _seg("code", "文件头部", 1, boundaries[0]["line"] - 1))
    return {"segments": segments, "outline": outline, "strategy": "indent"}


def _generic_chunks(lines: List[str]) -> List[Dict[str, Any]]:
    n = len(lines)
    segments: List[Dict[str, Any]] = []
    start = 1
    count = 0
    for i in range(n):
        count += 1
        is_blank = not lines[i].strip()
        at_target = count >= GENERIC_TARGET_LINES and is_blank
        at_max = count >= GENERIC_MAX_LINES
        if at_target or at_max or i == n - 1:
            end = i + 1
            segments.append(_seg("chunk", f"第 {start}~{end} 行", start, end))
            start = end + 1
            count = 0
    return segments


def _fill_gaps(segments: List[Dict[str, Any]], total_lines: int) -> None:
    """把注释/空行等未覆盖的行并入相邻分段（注释向下归属）。"""
    if not segments:
        return
    segments[0]["start_line"] = 1
    for i in range(1, len(segments)):
        prev_end = segments[i - 1]["end_line"]
        if segments[i]["start_line"] > prev_end + 1:
            segments[i]["start_line"] = prev_end + 1
    if segments[-1]["end_line"] < total_lines:
        segments[-1]["end_line"] = total_lines


def segment_file(source: str, ext: str) -> Dict[str, Any]:
    """返回 {language, strategy, segments:[{id,kind,title,start_line,end_line,code}], outline}"""
    lines = source.splitlines()
    total = len(lines)
    language = language_for(ext)

    result: Optional[Dict[str, Any]] = None
    if language == "python":
        result = segment_python_ast(source)
        if result is None:
            result = segment_python_indent(source)
    if result is None:
        result = {"segments": _generic_chunks(lines), "outline": [], "strategy": "generic"}

    segments = [s for s in result["segments"] if s["end_line"] >= s["start_line"]]
    segments.sort(key=lambda s: s["start_line"])
    _fill_gaps(segments, total)

    for idx, s in enumerate(segments):
        s["id"] = f"s{idx}"
        s["code"] = "\n".join(lines[s["start_line"] - 1:s["end_line"]])

    return {
        "language": language,
        "strategy": result["strategy"],
        "segments": segments,
        "outline": result.get("outline") or [],
        "total_lines": total,
    }
