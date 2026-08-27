"""面向代码解读的 Python 项目关系索引与上下文打包。

索引不仅记录类、函数和方法的位置，还记录模块导入、导入别名、符号调用、
局部对象的构造类型以及反向调用关系。解读和追问时据此提供三层信息：
项目地图、当前文件的上下游位置、与当前代码最相关的真实源码。
"""
import ast
import builtins
import hashlib
import keyword
import os
import re
import textwrap
import time
import warnings
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

# The evidence-index slice is intentionally additive.  These imports make the new
# persistent interfaces discoverable from the historical ``project_index`` module
# while every existing function below retains its original shape and behavior.
from .code_index import CodeIndex, IndexStatus  # noqa: F401
from .context_packer import ContextPacker  # noqa: F401
from .evidence import Evidence  # noqa: F401
from .retriever import Retriever  # noqa: F401
from .segmenter import SKIP_DIRS

MAX_FILES = 3000
MAX_FILE_BYTES = 1_200_000
TTL_SECONDS = 120.0

_cache: Dict[str, Dict[str, Any]] = {}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEF_RE = re.compile(
    r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_COMMON_NAMES = frozenset(keyword.kwlist) | frozenset(dir(builtins)) | {
    "self", "cls", "args", "kwargs", "np", "pd", "plt", "os", "sys", "re",
    "json", "time", "math", "logging", "typing", "pathlib",
}


def _decode(data: bytes) -> Optional[str]:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _norm(path: Optional[str]) -> str:
    return (path or "").replace("\\", "/")


def _same_file(left: Optional[str], right: Optional[str]) -> bool:
    return _norm(left).casefold() == _norm(right).casefold()


def _module_for_file(rel: str) -> str:
    path = _norm(rel)
    for suffix in (".pyw", ".py"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    parts = path.split("/")
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(p for p in parts if p)


def _resolve_import_module(current_module: str, module: Optional[str], level: int) -> str:
    if level <= 0:
        return module or ""
    package = current_module.split(".")[:-1]
    keep = max(0, len(package) - (level - 1))
    base = package[:keep]
    if module:
        base.extend(module.split("."))
    return ".".join(base)


def _attribute_parts(node: ast.AST) -> List[str]:
    parts: List[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return list(reversed(parts))


def _analyze_node(node: ast.AST) -> Tuple[List[Dict[str, str]], Dict[str, str]]:
    """提取调用表达式与 ``变量 = 构造器()`` 类型线索。"""
    refs: List[Dict[str, str]] = []
    local_types: Dict[str, str] = {}
    seen: Set[Tuple[str, str, str]] = set()

    for child in ast.walk(node):
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            value = child.value
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            if isinstance(value, ast.Call):
                parts = _attribute_parts(value.func)
                if parts:
                    for target in targets:
                        if isinstance(target, ast.Name):
                            local_types[target.id] = ".".join(parts)
        if not isinstance(child, ast.Call):
            continue
        parts = _attribute_parts(child.func)
        if not parts:
            continue
        if len(parts) == 1:
            ref = {"kind": "name", "name": parts[0], "receiver": ""}
        else:
            ref = {"kind": "attribute", "name": parts[-1],
                   "receiver": ".".join(parts[:-1])}
        key = (ref["kind"], ref["receiver"], ref["name"])
        if key not in seen:
            refs.append(ref)
            seen.add(key)
    return refs, local_types


def _extract_file(path: Path, rel: str) -> Optional[Dict[str, Any]]:
    try:
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            return None
        text = _decode(data)
        if text is None:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text)
    except Exception:
        return None

    lines = text.splitlines()
    module = _module_for_file(rel)
    imports: Dict[str, Dict[str, Optional[str]]] = {}

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                target_module = alias.name if alias.asname else alias.name.split(".")[0]
                imports[local] = {"module": target_module, "name": None}
        elif isinstance(node, ast.ImportFrom):
            target_module = _resolve_import_module(module, node.module, node.level)
            for alias in node.names:
                if alias.name == "*":
                    continue
                imports[alias.asname or alias.name] = {
                    "module": target_module,
                    "name": alias.name,
                }

    def make_entry(node: Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef], kind: str,
                   parent: Optional[str] = None) -> Dict[str, Any]:
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        sig = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else node.name
        doc = (ast.get_docstring(node) or "").strip()
        refs, local_types = _analyze_node(node)
        entry: Dict[str, Any] = {
            "name": node.name,
            "kind": kind,
            "file": rel,
            "module": module,
            "start_line": node.lineno,
            "end_line": end,
            "signature": sig[:180],
            "doc": doc.splitlines()[0][:120] if doc else "",
            "references": refs,
            "local_types": local_types,
        }
        if parent:
            entry["parent"] = parent
        return entry

    symbols: List[Dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(make_entry(node, "function"))
        elif isinstance(node, ast.ClassDef):
            entry = make_entry(node, "class")
            entry["methods"] = [
                child.name for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ][:25]
            symbols.append(entry)
            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if child.name.startswith("__") and child.name.endswith("__"):
                    continue
                symbols.append(make_entry(child, "method", parent=node.name))

    return {
        "file": rel,
        "module": module,
        "doc": (ast.get_docstring(tree) or "").strip().splitlines()[0][:160]
        if ast.get_docstring(tree) else "",
        "imports": imports,
        "symbols": symbols,
        "source_hash": hashlib.sha256(data).hexdigest(),
    }


def _entry_key(entry: Dict[str, Any]) -> str:
    return ":".join([
        _norm(entry["file"]), str(entry["start_line"]), entry["kind"],
        entry.get("parent", ""), entry["name"],
    ])


def _defs_in_module(index: Dict[str, Any], module: str, name: str) -> List[Dict[str, Any]]:
    return [entry for entry in index["symbols"].get(name, [])
            if entry.get("module") == module]


def _file_info(index: Dict[str, Any], rel_file: Optional[str]) -> Optional[Dict[str, Any]]:
    for path, info in index.get("file_info", {}).items():
        if _same_file(path, rel_file):
            return info
    return None


def _resolve_name(index: Dict[str, Any], name: str,
                  current_file: Optional[str]) -> Optional[Dict[str, Any]]:
    info = _file_info(index, current_file)
    if info:
        binding = info["imports"].get(name)
        if binding and binding.get("name"):
            matches = _defs_in_module(index, binding["module"], binding["name"])
            if matches:
                return matches[0]

        for entry in index["symbols"].get(name, []):
            if _same_file(entry["file"], current_file):
                return entry

        dep_modules = {dep["module"] for dep in info.get("dependencies", [])}
        for entry in index["symbols"].get(name, []):
            if entry.get("module") in dep_modules:
                return entry

    matches = index["symbols"].get(name, [])
    return matches[0] if matches else None


def _resolve_constructor(index: Dict[str, Any], expression: str,
                         current_file: Optional[str]) -> Optional[Dict[str, Any]]:
    parts = expression.split(".")
    if len(parts) == 1:
        entry = _resolve_name(index, parts[0], current_file)
        return entry if entry and entry["kind"] == "class" else None
    info = _file_info(index, current_file)
    if info:
        binding = info["imports"].get(parts[0])
        if binding and binding.get("name") is None:
            module = ".".join([binding["module"]] + parts[1:-1])
            matches = _defs_in_module(index, module, parts[-1])
            if matches and matches[0]["kind"] == "class":
                return matches[0]
    return None


def _resolve_reference(index: Dict[str, Any], ref: Dict[str, str],
                       current_file: Optional[str], local_types: Dict[str, str],
                       owner: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if ref["kind"] == "name":
        return _resolve_name(index, ref["name"], current_file)

    receiver = ref.get("receiver", "")
    info = _file_info(index, current_file)
    if receiver == "self" and owner and owner.get("parent"):
        for entry in index["symbols"].get(ref["name"], []):
            if (_same_file(entry["file"], current_file)
                    and entry.get("parent") == owner["parent"]):
                return entry

    constructor = local_types.get(receiver)
    if constructor:
        cls = _resolve_constructor(index, constructor, current_file)
        if cls:
            for entry in index["symbols"].get(ref["name"], []):
                if (entry.get("parent") == cls["name"]
                        and entry.get("module") == cls.get("module")):
                    return entry

    receiver_root = receiver.split(".")[0] if receiver else ""
    if info and receiver_root:
        binding = info["imports"].get(receiver_root)
        if binding:
            if binding.get("name") is None:
                suffix = receiver.split(".")[1:]
                module = ".".join([binding["module"]] + suffix)
                matches = _defs_in_module(index, module, ref["name"])
                if matches:
                    return matches[0]
            else:
                classes = _defs_in_module(index, binding["module"], binding["name"])
                if classes and classes[0]["kind"] == "class":
                    for entry in index["symbols"].get(ref["name"], []):
                        if (entry.get("parent") == classes[0]["name"]
                                and entry.get("module") == classes[0].get("module")):
                            return entry

    methods = [entry for entry in index["symbols"].get(ref["name"], [])
               if entry["kind"] == "method"]
    return methods[0] if len(methods) == 1 else None


def _resolve_entry_references(index: Dict[str, Any],
                              entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for ref in entry.get("references", []):
        target = _resolve_reference(
            index, ref, entry["file"], entry.get("local_types", {}), entry)
        if target and _entry_key(target) not in seen and _entry_key(target) != _entry_key(entry):
            out.append(target)
            seen.add(_entry_key(target))
    return out


def build_index(root: str) -> Dict[str, Any]:
    root_path = Path(root)
    symbols: Dict[str, List[Dict[str, Any]]] = {}
    file_info: Dict[str, Dict[str, Any]] = {}
    modules: Dict[str, str] = {}
    t0 = time.time()

    for dirpath, dirnames, filenames in os.walk(str(root_path)):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for filename in sorted(filenames):
            if len(file_info) >= MAX_FILES:
                break
            if not filename.endswith((".py", ".pyw")):
                continue
            path = Path(dirpath) / filename
            try:
                rel = str(path.relative_to(root_path))
            except ValueError:
                rel = str(path)
            extracted = _extract_file(path, rel)
            if extracted is None:
                continue
            file_info[rel] = extracted
            modules[extracted["module"]] = rel
            for entry in extracted["symbols"]:
                symbols.setdefault(entry["name"], []).append(entry)
        if len(file_info) >= MAX_FILES:
            break

    index: Dict[str, Any] = {
        "root": str(root_path),
        "symbols": symbols,
        "file_info": file_info,
        "modules": modules,
        "files": len(file_info),
        "built_at": time.time(),
        "build_ms": int((time.time() - t0) * 1000),
    }

    # ``from package import submodule as alias`` 在 AST 中和导入同名符号形态相同。
    # 若 package.submodule 确实存在于项目，优先把它解释为模块别名。
    for info in file_info.values():
        for binding in info["imports"].values():
            imported_name = binding.get("name")
            if not imported_name:
                continue
            candidate_module = ".".join(
                part for part in (binding["module"], imported_name) if part)
            if candidate_module in modules:
                binding["module"] = candidate_module
                binding["name"] = None

    for info in file_info.values():
        dependencies: List[Dict[str, str]] = []
        seen_deps: Set[str] = set()
        for binding in info["imports"].values():
            module = binding["module"]
            dep_file = modules.get(module)
            if dep_file and not _same_file(dep_file, info["file"]) and dep_file not in seen_deps:
                dependencies.append({"module": module, "file": dep_file})
                seen_deps.add(dep_file)
        info["dependencies"] = dependencies
        info["defined"] = [entry["name"] for entry in info["symbols"]
                           if entry["kind"] != "method"]

    reverse: Dict[str, List[Dict[str, Any]]] = {}
    for entries in symbols.values():
        for caller in entries:
            for target in _resolve_entry_references(index, caller):
                callers = reverse.setdefault(_entry_key(target), [])
                if all(_entry_key(item) != _entry_key(caller) for item in callers):
                    callers.append(caller)
    index["reverse_refs"] = reverse

    fingerprint_parts = [
        _norm(path) + ":" + info["source_hash"]
        for path, info in sorted(file_info.items(), key=lambda item: _norm(item[0]))
    ]
    index["fingerprint"] = hashlib.sha256(
        "|".join(fingerprint_parts).encode("utf-8")).hexdigest()
    return index


def get_index(root: Optional[str]) -> Optional[Dict[str, Any]]:
    if not root:
        return None
    try:
        root_norm = str(Path(root).resolve())
        if not Path(root_norm).is_dir():
            return None
    except Exception:
        return None
    index = _cache.get(root_norm)
    if index is None or time.time() - index["built_at"] > TTL_SECONDS:
        index = build_index(root_norm)
        _cache[root_norm] = index
    return index


def context_signature(index: Optional[Dict[str, Any]]) -> str:
    """缓存键使用的项目上下文版本；无项目时返回稳定空串。"""
    return str(index.get("fingerprint", "")) if index else ""


def _entry_title(entry: Dict[str, Any]) -> str:
    if entry["kind"] == "class":
        return f"类 {entry['name']}"
    if entry["kind"] == "method":
        return f"类 {entry.get('parent', '?')} 的方法 {entry['name']}"
    return f"函数 {entry['name']}"


def _format_entry(entry: Dict[str, Any]) -> str:
    head = (f"- {_entry_title(entry)} —— 定义于 {_norm(entry['file'])} "
            f"第 {entry['start_line']}~{entry['end_line']} 行")
    body = f"  {entry['signature']}"
    if entry.get("doc"):
        body += f"  # {entry['doc']}"
    lines = [head, body]
    if entry.get("methods"):
        lines.append("  主要方法: " + ", ".join(entry["methods"]))
    return "\n".join(lines)


def _analyze_code(code: str) -> Tuple[List[Dict[str, str]], Dict[str, str]]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(textwrap.dedent(code))
        return _analyze_node(tree)
    except Exception:
        return [], {}


def _ordered_identifiers(code: str) -> Iterable[str]:
    seen: Set[str] = set()
    for match in _IDENT_RE.finditer(code):
        name = match.group(0)
        if name in seen or name in _COMMON_NAMES or len(name) < 2:
            continue
        seen.add(name)
        yield name


def _resolve_code_entries(index: Dict[str, Any], code: str,
                          current_file: Optional[str]) -> List[Dict[str, Any]]:
    refs, local_types = _analyze_code(code)
    entries: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    def add(entry: Optional[Dict[str, Any]]) -> None:
        if entry and _entry_key(entry) not in seen:
            entries.append(entry)
            seen.add(_entry_key(entry))

    for ref in refs:
        add(_resolve_reference(index, ref, current_file, local_types))
    for name in _ordered_identifiers(code):
        add(_resolve_name(index, name, current_file))
    return entries


def related_context(index: Optional[Dict[str, Any]], code: str,
                    current_rel_file: Optional[str], max_symbols: int = 6,
                    max_chars: int = 1800) -> str:
    """兼容轻量摘要接口：返回当前片段直接引用的跨文件定义。"""
    if not index or not index.get("symbols"):
        return ""
    defined_here = set(_DEF_RE.findall(code))
    blocks: List[str] = []
    total = 0
    for entry in _resolve_code_entries(index, code, current_rel_file):
        if entry["name"] in defined_here or _same_file(entry["file"], current_rel_file):
            continue
        block = _format_entry(entry)
        if total + len(block) > max_chars:
            break
        blocks.append(block)
        total += len(block)
        if len(blocks) >= max_symbols:
            break
    return "\n".join(blocks)


def _read_source_lines(root: str, entry: Dict[str, Any],
                       max_lines: int) -> Optional[str]:
    try:
        path = Path(entry["file"])
        if not path.is_absolute():
            path = Path(root) / path
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            return None
        text = _decode(data)
        if text is None:
            return None
        lines = text.splitlines()
        start, end = int(entry["start_line"]), int(entry["end_line"])
        if start < 1 or start > len(lines):
            return None
        picked = lines[start - 1:min(end, len(lines))]
        if len(picked) > max_lines:
            picked = picked[:max_lines]
            picked.append(f"# …（实现共 {end - start + 1} 行，超出部分已截断）")
        return "\n".join(picked)
    except Exception:
        return None


def _source_block(root: str, entry: Dict[str, Any], max_lines: int) -> str:
    head = (f"### {_entry_title(entry)} —— 定义于 {_norm(entry['file'])} "
            f"第 {entry['start_line']}~{entry['end_line']} 行")
    source = _read_source_lines(root, entry, max_lines)
    return _format_entry(entry) if source is None else f"{head}\n```python\n{source}\n```"


def related_sources(index: Optional[Dict[str, Any]], code: str,
                    current_rel_file: Optional[str], root: Optional[str] = None,
                    max_symbols: int = 6, max_lines_each: int = 80,
                    max_chars: int = 8000, dependency_depth: int = 2) -> str:
    """返回直接命中、递归依赖与上游调用方的真实源码。"""
    if not index or not index.get("symbols") or max_chars <= 0:
        return ""
    root_dir = root or index.get("root") or ""
    initial = _resolve_code_entries(index, code, current_rel_file)
    defined_names = set(_DEF_RE.findall(code))
    for name in defined_names:
        entry = _resolve_name(index, name, current_rel_file)
        if entry and all(_entry_key(item) != _entry_key(entry) for item in initial):
            initial.insert(0, entry)

    selected: List[Tuple[Dict[str, Any], int]] = []
    seen: Set[str] = set()
    queue: List[Tuple[Dict[str, Any], int]] = [(entry, 0) for entry in initial]
    while queue and len(selected) < max_symbols:
        entry, depth = queue.pop(0)
        key = _entry_key(entry)
        if key in seen:
            continue
        selected.append((entry, depth))
        seen.add(key)
        if depth < dependency_depth:
            for target in _resolve_entry_references(index, entry):
                if _entry_key(target) not in seen:
                    queue.append((target, depth + 1))

    blocks: List[str] = []
    total = 0
    for entry, depth in selected:
        relation = "直接相关" if depth == 0 else f"第 {depth} 层依赖"
        block = f"<!-- {relation} -->\n" + _source_block(root_dir, entry, max_lines_each)
        if total + len(block) > max_chars:
            summary = f"<!-- {relation} -->\n" + _format_entry(entry)
            if total + len(summary) > max_chars:
                break
            block = summary
        blocks.append(block)
        total += len(block) + 2

    caller_lines: List[str] = []
    caller_entries: List[Dict[str, Any]] = []
    for entry in initial:
        for caller in index.get("reverse_refs", {}).get(_entry_key(entry), []):
            line = (f"- 调用方 {_entry_title(caller)}：{_norm(caller['file'])} "
                    f"第 {caller['start_line']}~{caller['end_line']} 行")
            if line not in caller_lines:
                caller_lines.append(line)
                caller_entries.append(caller)
    if caller_lines:
        caller_parts = ["## 上游调用方", "\n".join(caller_lines[:12])]
        for caller in caller_entries[:3]:
            source = _source_block(root_dir, caller, min(max_lines_each, 50))
            candidate = "\n\n".join(caller_parts + [source])
            if total + len(candidate) > max_chars:
                break
            caller_parts.append(source)
        caller_block = "\n\n".join(caller_parts)
        if total + len(caller_block) <= max_chars:
            blocks.append(caller_block)

    return "\n\n".join(blocks)


def _transitive_dependencies(index: Dict[str, Any], start: Dict[str, Any],
                             depth: int = 2) -> List[Tuple[Dict[str, Any], int]]:
    result: List[Tuple[Dict[str, Any], int]] = []
    seen = {_norm(start["file"]).casefold()}
    queue: List[Tuple[Dict[str, Any], int]] = [(start, 0)]
    while queue:
        info, current_depth = queue.pop(0)
        if current_depth >= depth:
            continue
        for dep in info.get("dependencies", []):
            dep_info = _file_info(index, dep["file"])
            key = _norm(dep["file"]).casefold()
            if dep_info and key not in seen:
                seen.add(key)
                result.append((dep_info, current_depth + 1))
                queue.append((dep_info, current_depth + 1))
    return result


def project_overview(index: Optional[Dict[str, Any]], current_rel_file: Optional[str],
                     max_chars: int = 4500) -> str:
    """Architecture-first project map plus the current file's graph position."""
    if not index or max_chars <= 0:
        return ""
    file_info = index.get("file_info", {})
    files = sorted(file_info, key=lambda path: _norm(path).casefold())

    inbound: Dict[str, int] = {path: 0 for path in files}
    for info in file_info.values():
        for dependency in info.get("dependencies", []):
            dep = dependency.get("file")
            if dep in inbound:
                inbound[dep] += 1

    entry_names = {"main.py", "__main__.py", "app.py", "run.py", "cli.py", "manage.py"}
    entrypoints = [
        path for path in files
        if Path(path).name.casefold() in entry_names
        or any(entry.get("name") == "main" for entry in file_info[path].get("symbols", []))
    ]
    central = sorted(
        (path for path in files if Path(path).name != "__init__.py"),
        key=lambda path: (
            -inbound.get(path, 0),
            -len(file_info[path].get("symbols", [])),
            -len(file_info[path].get("dependencies", [])),
            _norm(path).casefold(),
        ),
    )[:8]
    public_api: List[str] = []
    for path in central:
        for entry in file_info[path].get("symbols", []):
            name = str(entry.get("name", ""))
            if name and not name.startswith("_") and entry.get("kind") != "method":
                public_api.append(f"{name}（{_norm(path)}）")
            if len(public_api) >= 12:
                break
        if len(public_api) >= 12:
            break
    tests = [path for path in files if Path(path).name.startswith("test_")
             or "tests" in {part.casefold() for part in Path(path).parts}]
    lines = [
        "## 项目全貌",
        f"- 项目包含 {index.get('files', 0)} 个可解析 Python 文件、"
        f"{sum(len(v) for v in index.get('symbols', {}).values())} 个类/函数/方法符号。",
        "- 入口点：" + ("、".join(_norm(path) for path in entrypoints[:10])
                       if entrypoints else "未识别到常见入口文件"),
        "- 中心模块：" + ("、".join(
            f"{_norm(path)}（{inbound.get(path, 0)} 个上游模块）" for path in central)
            if central else "未识别"),
        "- 公共 API：" + ("、".join(public_api) if public_api else "未识别公开符号"),
        "- 测试组织：" + ("、".join(_norm(path) for path in tests[:12])
                         if tests else "未发现 Python 测试文件"),
    ]

    current = _file_info(index, current_rel_file)
    if current:
        lines.extend(["", "## 当前文件在项目中的位置",
                      f"- 当前文件：{_norm(current['file'])}（模块 {current['module'] or '<root>'}）"])
        if current.get("doc"):
            lines.append(f"- 模块职责：{current['doc']}")
        if current.get("defined"):
            lines.append("- 对外定义：" + "、".join(current["defined"][:25]))
        dependencies = _transitive_dependencies(index, current, depth=2)
        if dependencies:
            lines.append("- 项目内依赖链：")
            for info, depth in dependencies[:20]:
                desc = f"（{info['doc']}）" if info.get("doc") else ""
                lines.append(f"  - 第 {depth} 层依赖 {_norm(info['file'])}{desc}")
        dependents = [info for info in index["file_info"].values()
                      if any(_same_file(dep["file"], current["file"])
                             for dep in info.get("dependencies", []))]
        if dependents:
            lines.append("- 被以下文件引用：" + "、".join(
                _norm(info["file"]) for info in dependents[:20]))

        caller_lines: List[str] = []
        for entry in current["symbols"]:
            for caller in index.get("reverse_refs", {}).get(_entry_key(entry), []):
                line = (f"  - {_norm(caller['file'])} 中的 {_entry_title(caller)} "
                        f"调用当前文件的 {_entry_title(entry)}")
                if line not in caller_lines:
                    caller_lines.append(line)
        if caller_lines:
            lines.append("- 已识别的上游调用：")
            lines.extend(caller_lines[:15])

    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    suffix = "\n- …（项目地图受上下文预算限制，已省略较低优先级文件）"
    return text[:max(0, max_chars - len(suffix))] + suffix


def build_project_context(index: Optional[Dict[str, Any]], code: str,
                          current_rel_file: Optional[str], root: Optional[str] = None,
                          question: str = "", max_chars: int = 10000,
                          max_symbols: int = 8, dependency_depth: int = 2) -> str:
    """按总预算组合项目地图与相关源码，供解读、总览和追问共同使用。"""
    if not index or max_chars <= 0:
        return ""
    overview_budget = min(4500, max(1200, int(max_chars * 0.38)))
    overview = project_overview(index, current_rel_file, overview_budget)
    header = "## 关联源码"
    remaining = max_chars - len(overview) - len(header) - 4
    sources = related_sources(
        index, code + ("\n" + question if question else ""), current_rel_file, root,
        max_symbols=max_symbols, max_chars=max(0, remaining),
        dependency_depth=dependency_depth,
    )
    parts = [overview]
    if sources:
        parts.append(header + "\n" + sources)
    return "\n\n".join(part for part in parts if part)[:max_chars]


def relative_of(root: Optional[str], file_path: str) -> Optional[str]:
    if not root:
        return None
    try:
        return str(Path(file_path).resolve().relative_to(Path(root).resolve()))
    except Exception:
        return None
