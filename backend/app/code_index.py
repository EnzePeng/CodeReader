"""Persistent, incremental, Python-first evidence index.

The index combines compiler-like Python facts with FTS5 text coverage.  Files that
cannot be parsed (including syntax newer than the running interpreter) are still
chunked and searchable, and their parse failure is exposed through ``IndexStatus``.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple, Union
from uuid import uuid4

SCHEMA_VERSION = 2
MAX_INDEX_FILE_BYTES = 2_000_000
TEXT_CHUNK_LINES = 80
TEXT_CHUNK_OVERLAP = 10

_LANGUAGE_BY_EXT = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".php": "php", ".swift": "swift", ".scala": "scala",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell",
    ".bat": "bat", ".cmd": "bat", ".sql": "sql",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".vue": "vue", ".svelte": "svelte",
    ".json": "json", ".jsonc": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "ini",
    ".xml": "xml", ".md": "markdown", ".markdown": "markdown",
    ".rst": "rst", ".txt": "plaintext", ".lock": "plaintext",
    ".gradle": "gradle", ".properties": "properties", ".proto": "protobuf",
    ".graphql": "graphql", ".gql": "graphql", ".lua": "lua", ".r": "r",
}

_SPECIAL_TEXT_NAMES = {
    "readme", "license", "copying", "notice", "changelog", "authors",
    "dockerfile", "makefile", "rakefile", "gemfile", "procfile",
    "requirements.txt", "constraints.txt", "pipfile", "poetry.lock",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "cargo.toml",
    "cargo.lock", "go.mod", "go.sum", "pom.xml", "build.gradle",
    "build.gradle.kts", "composer.json", "composer.lock",
}

_HARD_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".venv", "venv", "env",
    "site-packages", "dist", "build", "target", ".next", ".nuxt",
    "$RECYCLE.BIN", "System Volume Information",
}


@dataclass(frozen=True)
class IndexStatus:
    root: str
    project_id: int
    schema_version: int = SCHEMA_VERSION
    indexed_files: int = 0
    added_files: int = 0
    updated_files: int = 0
    reused_files: int = 0
    hash_confirmed_files: int = 0
    removed_files: int = 0
    skipped_files: List[str] = field(default_factory=list)
    parse_errors: Dict[str, str] = field(default_factory=dict)
    languages: Dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0
    updated_at: float = 0.0

    @property
    def total_files(self) -> int:
        return sum(self.languages.values())

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["total_files"] = self.total_files
        return payload


@dataclass(frozen=True)
class _IgnoreRule:
    pattern: str
    negated: bool
    directory_only: bool
    anchored: bool

    def matches(self, relative_path: str, is_dir: bool) -> bool:
        path = relative_path.strip("/")
        if not path:
            return False
        parts = path.split("/")
        # Prefixes let directory rules cover all descendants.
        candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        if self.directory_only and not is_dir:
            candidates = candidates[:-1]
        pattern = self.pattern.strip("/")
        if not pattern:
            return False
        if "/" not in pattern and not self.anchored:
            candidate_parts = parts if not self.directory_only else (parts if is_dir else parts[:-1])
            return any(fnmatch.fnmatchcase(part, pattern) for part in candidate_parts)
        if self.anchored:
            return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates)
        return any(
            fnmatch.fnmatchcase(candidate, pattern)
            or fnmatch.fnmatchcase(candidate, "*/" + pattern)
            for candidate in candidates
        )


class _IgnoreMatcher:
    def __init__(self, root: Path) -> None:
        self.rules: List[_IgnoreRule] = []
        for name in (".gitignore", ".codereaderignore"):
            path = root / name
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            except OSError:
                continue
            for raw in lines:
                line = raw.rstrip()
                if not line or line.lstrip().startswith("#"):
                    continue
                negated = line.startswith("!")
                if negated:
                    line = line[1:]
                elif line.startswith(r"\!"):
                    line = line[1:]
                if line.startswith(r"\#"):
                    line = line[1:]
                directory_only = line.endswith("/")
                anchored = line.startswith("/")
                line = line.strip("/")
                if line:
                    self.rules.append(_IgnoreRule(line, negated, directory_only, anchored))

    def ignored(self, relative_path: str, is_dir: bool = False) -> bool:
        ignored = False
        for rule in self.rules:
            if rule.matches(relative_path.replace("\\", "/"), is_dir):
                ignored = not rule.negated
        return ignored


def _decode(data: bytes) -> Optional[str]:
    if b"\x00" in data[:8192]:
        return None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _language(path: Path) -> Optional[str]:
    suffix = path.suffix.casefold()
    if suffix in _LANGUAGE_BY_EXT:
        return _LANGUAGE_BY_EXT[suffix]
    name = path.name.casefold()
    if name in _SPECIAL_TEXT_NAMES or name.startswith("readme.") or name.startswith("license."):
        return "plaintext"
    return None


def _node_start(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", None) or []
    values = [getattr(node, "lineno", 1)] + [getattr(item, "lineno", 1) for item in decorators]
    return min(values)


def _call_parts(node: ast.AST) -> List[str]:
    parts: List[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if (isinstance(current, ast.Call) and isinstance(current.func, ast.Name)
            and current.func.id == "super"):
        parts.append("super")
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return list(reversed(parts))


def _module_name(relative_path: str) -> str:
    """Return the importable Python module represented by a project path."""
    normalized = relative_path.replace("\\", "/")
    if normalized.endswith(".py"):
        normalized = normalized[:-3]
    parts = [part for part in normalized.split("/") if part]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative_module(
    current_module: str,
    is_package: bool,
    imported_module: Optional[str],
    level: int,
) -> str:
    if level <= 0:
        return imported_module or ""
    package = current_module.split(".") if current_module else []
    if not is_package and package:
        package.pop()
    ascend = max(0, level - 1)
    if ascend:
        package = package[:-ascend] if ascend < len(package) else []
    if imported_module:
        package.extend(part for part in imported_module.split(".") if part)
    return ".".join(package)


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, source: str, module_name: str, is_package: bool) -> None:
        self.lines = source.splitlines()
        self.module_name = module_name
        self.is_package = is_package
        self.stack: List[Tuple[str, str]] = []
        self.alias_scopes: List[Dict[str, Tuple[str, str]]] = [{}]
        self.type_scopes: List[Dict[str, Tuple[str, str]]] = [{}]
        self.attribute_types: Dict[Tuple[str, str], Tuple[str, str]] = {}
        self.symbols: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []
        self.imports: List[Dict[str, Any]] = []

    def _scope_name(self) -> str:
        return ".".join(item[0] for item in self.stack)

    def _lookup_alias(self, name: str) -> Optional[Tuple[str, str]]:
        for aliases in reversed(self.alias_scopes):
            binding = aliases.get(name)
            if binding is not None:
                return binding
        return None

    def _class_scope(self) -> str:
        return ".".join(name for name, kind in self.stack if kind == "class")

    def _type_from_expr(self, node: Optional[ast.AST]) -> Optional[Tuple[str, str]]:
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode="eval").body
            except (SyntaxError, ValueError):
                return None
        if isinstance(node, ast.Subscript):
            return self._type_from_expr(node.slice)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._type_from_expr(node.left) or self._type_from_expr(node.right)
        parts = _call_parts(node)
        if not parts:
            return None
        binding = self._lookup_alias(parts[0])
        if len(parts) == 1:
            if binding is not None:
                module, imported = binding
                return module, imported or parts[0]
            return self.module_name, parts[0]
        if binding is not None:
            module, imported = binding
            module_parts = [module]
            if imported:
                module_parts.append(imported)
            module_parts.extend(parts[1:-1])
            return ".".join(part for part in module_parts if part), parts[-1]
        return self.module_name, ".".join(parts)

    def _lookup_type(self, receiver: str) -> Optional[Tuple[str, str]]:
        for values in reversed(self.type_scopes):
            if receiver in values:
                return values[receiver]
        class_scope = self._class_scope()
        return self.attribute_types.get((class_scope, receiver))

    def _remember_type(self, target: ast.AST, value: Optional[Tuple[str, str]]) -> None:
        if value is None:
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._remember_type(item, value)
            return
        parts = _call_parts(target)
        if not parts:
            return
        name = ".".join(parts)
        if len(parts) == 1:
            self.type_scopes[-1][name] = value
        elif parts[0] in {"self", "cls"} and self._class_scope():
            self.attribute_types[(self._class_scope(), name)] = value

    def _add_symbol(self, node: ast.AST, name: str, kind: str) -> str:
        qualified = ".".join([item[0] for item in self.stack] + [name])
        start = _node_start(node)
        end_lineno = getattr(node, "end_lineno", None)
        end = end_lineno if isinstance(end_lineno, int) else start
        signature = self.lines[getattr(node, "lineno", start) - 1].strip() if self.lines else name
        try:
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = (ast.get_docstring(node) or "").strip().splitlines()[0][:240]
            else:
                doc = ""
        except (TypeError, IndexError):
            doc = ""
        self.symbols.append({
            "name": name,
            "qualified_name": qualified,
            "symbol_key": f"{self.module_name}:{qualified}",
            "kind": kind,
            "start_line": start,
            "end_line": end,
            "signature": signature[:500],
            "doc": doc,
            "parent": self.stack[-1][0] if self.stack else "",
            "bases": [ast.unparse(base) for base in getattr(node, "bases", [])],
        })
        return qualified

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add_symbol(node, node.name, "class")
        self.stack.append((node.name, "class"))
        self.alias_scopes.append({})
        self.type_scopes.append({})
        self.generic_visit(node)
        self.type_scopes.pop()
        self.alias_scopes.pop()
        self.stack.pop()

    def _visit_function(self, node: ast.AST) -> None:
        name = str(getattr(node, "name"))
        kind = "method" if self.stack and self.stack[-1][1] == "class" else "function"
        self._add_symbol(node, name, kind)
        self.stack.append((name, kind))
        self.alias_scopes.append({})
        self.type_scopes.append({})
        arguments = getattr(node, "args", None)
        if arguments is not None:
            for argument in list(arguments.posonlyargs) + list(arguments.args) + list(arguments.kwonlyargs):
                value = self._type_from_expr(argument.annotation)
                if value is not None:
                    self.type_scopes[-1][argument.arg] = value
        self.generic_visit(node)
        self.type_scopes.pop()
        self.alias_scopes.pop()
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_TypeAlias(self, node: ast.AST) -> None:  # Python 3.12+; harmless on older runtimes
        target = getattr(node, "name", None)
        name = getattr(target, "id", None) or getattr(target, "name", None)
        if name:
            self._add_symbol(node, str(name), "type_alias")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                local_name = alias.asname
                bound_module = alias.name
            else:
                local_name = alias.name.split(".")[0]
                bound_module = local_name
            self.alias_scopes[-1][local_name] = (bound_module, "")
            self.imports.append({
                "binding_scope": self._scope_name(),
                "module": alias.name,
                "imported_name": "",
                "local_name": local_name,
                "level": 0,
                "is_star": 0,
                "start_line": int(getattr(node, "lineno", 1) or 1),
                "end_line": int(
                    getattr(node, "end_lineno", None) or getattr(node, "lineno", 1) or 1
                ),
            })

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = _resolve_relative_module(
            self.module_name,
            self.is_package,
            node.module,
            int(getattr(node, "level", 0) or 0),
        )
        for alias in node.names:
            is_star = alias.name == "*"
            local_name = alias.asname or alias.name
            if not is_star:
                self.alias_scopes[-1][local_name] = (module, alias.name)
            self.imports.append({
                "binding_scope": self._scope_name(),
                "module": module,
                "imported_name": alias.name,
                "local_name": local_name,
                "level": int(getattr(node, "level", 0) or 0),
                "is_star": int(is_star),
                "start_line": int(getattr(node, "lineno", 1) or 1),
                "end_line": int(
                    getattr(node, "end_lineno", None) or getattr(node, "lineno", 1) or 1
                ),
            })

    def visit_Assign(self, node: ast.Assign) -> None:
        value = self._type_from_expr(node.value.func) if isinstance(node.value, ast.Call) else None
        for target in node.targets:
            self._remember_type(target, value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        value = self._type_from_expr(node.annotation)
        if value is None and isinstance(node.value, ast.Call):
            value = self._type_from_expr(node.value.func)
        self._remember_type(node.target, value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        parts = _call_parts(node.func)
        if parts:
            raw_name = parts[-1]
            target_name = raw_name
            target_module = self.module_name
            target_qualified = ""
            root_binding = self._lookup_alias(parts[0])
            if len(parts) == 1 and root_binding is not None:
                target_module, imported_name = root_binding
                target_name = imported_name or raw_name
            elif len(parts) > 1 and root_binding is not None:
                bound_module, imported_name = root_binding
                module_parts = [bound_module]
                if imported_name:
                    module_parts.append(imported_name)
                module_parts.extend(parts[1:-1])
                target_module = ".".join(part for part in module_parts if part)
            elif len(parts) == 2 and parts[0] in {"self", "cls", "super"}:
                class_names = [name for name, kind in self.stack if kind == "class"]
                if class_names:
                    prefix = "super:" if parts[0] == "super" else ""
                    target_qualified = prefix + ".".join(class_names + [raw_name])
            elif len(parts) > 1:
                inferred = self._lookup_type(".".join(parts[:-1]))
                if inferred is not None:
                    target_module, instance_type = inferred
                    target_qualified = f"{instance_type}.{raw_name}"
                else:
                    # Do not pretend an unknown receiver belongs to this module.
                    target_module = ""
            self.calls.append({
                "caller_qualified": self._scope_name(),
                "callee_name": target_name,
                "callee_expr": ".".join(parts),
                "target_module": target_module,
                "target_qualified": target_qualified,
                "receiver": ".".join(parts[:-1]),
                "start_line": int(getattr(node, "lineno", 1) or 1),
                "end_line": int(
                    getattr(node, "end_lineno", None) or getattr(node, "lineno", 1) or 1
                ),
            })
        self.generic_visit(node)


def _extract_python(
    source: str,
    module_name: str = "",
    is_package: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], str]:
    try:
        # On Python 3.13 this naturally accepts 3.13 nodes.  Older runtimes report a
        # parse error, after which the file remains fully available through FTS.
        tree = ast.parse(source, type_comments=True)
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        message = f"{exc.__class__.__name__}: {exc}"
        return [], [], [], message[:1000]
    visitor = _PythonVisitor(source, module_name, is_package)
    visitor.visit(tree)
    return visitor.symbols, visitor.calls, visitor.imports, ""


def _chunks(source: str) -> Iterable[Tuple[int, int, str, str]]:
    lines = source.splitlines()
    if not lines:
        return
    start = 0
    while start < len(lines):
        end = min(len(lines), start + TEXT_CHUNK_LINES)
        content = "\n".join(lines[start:end])
        yield start + 1, end, content, hashlib.sha256(content.encode("utf-8")).hexdigest()
        if end >= len(lines):
            break
        start = max(start + 1, end - TEXT_CHUNK_OVERLAP)


class CodeIndex:
    """SQLite-backed incremental project index."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._prepare_schema_file()
        with self._connect() as conn:
            self._ensure_schema(conn)

    def _prepare_schema_file(self) -> None:
        """Build incompatible derived caches separately, then atomically replace them."""
        if not self.db_path.exists():
            return
        version: Optional[int] = None
        current: Optional[sqlite3.Connection] = None
        try:
            current = sqlite3.connect(str(self.db_path), timeout=10.0)
            exists = current.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if exists:
                row = current.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
                version = int(row[0]) if row else None
            current.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (sqlite3.Error, OSError, ValueError):
            version = None
        finally:
            if current is not None:
                current.close()
        if version == SCHEMA_VERSION:
            return
        temporary = self.db_path.with_name(
            f"{self.db_path.name}.rebuild-{uuid4().hex}.tmp")
        try:
            conn = sqlite3.connect(str(temporary), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=DELETE")
            try:
                self._ensure_schema(conn)
            finally:
                conn.close()
            os.replace(temporary, self.db_path)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.db_path) + suffix)
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        version = None
        if exists:
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            version = int(row[0]) if row else None
        if version != SCHEMA_VERSION:
            conn.executescript("""
                DROP TRIGGER IF EXISTS text_chunks_ai;
                DROP TRIGGER IF EXISTS text_chunks_ad;
                DROP TRIGGER IF EXISTS text_chunks_au;
                DROP TABLE IF EXISTS text_chunks_fts;
                DROP TABLE IF EXISTS calls;
                DROP TABLE IF EXISTS imports;
                DROP TABLE IF EXISTS symbols;
                DROP TABLE IF EXISTS text_chunks;
                DROP TABLE IF EXISTS files;
                DROP TABLE IF EXISTS projects;
                DROP TABLE IF EXISTS schema_version;
            """)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                root TEXT NOT NULL UNIQUE COLLATE NOCASE,
                last_indexed REAL NOT NULL DEFAULT 0,
                generation INTEGER NOT NULL DEFAULT 0,
                schema_version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                path TEXT NOT NULL,
                module_name TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                source_hash TEXT NOT NULL,
                parse_error TEXT NOT NULL DEFAULT '',
                indexed_at REAL NOT NULL,
                line_count INTEGER NOT NULL,
                UNIQUE(project_id, path)
            );
            CREATE INDEX IF NOT EXISTS files_project_path ON files(project_id, path);
            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                symbol_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                signature TEXT NOT NULL DEFAULT '',
                doc TEXT NOT NULL DEFAULT '',
                parent TEXT NOT NULL DEFAULT '',
                bases TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name COLLATE NOCASE);
            CREATE UNIQUE INDEX IF NOT EXISTS symbols_key ON symbols(file_id, symbol_key);
            CREATE INDEX IF NOT EXISTS symbols_file ON symbols(file_id);
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                binding_scope TEXT NOT NULL DEFAULT '',
                module TEXT NOT NULL DEFAULT '',
                imported_name TEXT NOT NULL DEFAULT '',
                local_name TEXT NOT NULL DEFAULT '',
                level INTEGER NOT NULL DEFAULT 0,
                is_star INTEGER NOT NULL DEFAULT 0,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS imports_file ON imports(file_id);
            CREATE INDEX IF NOT EXISTS imports_module ON imports(module COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                caller_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
                target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
                callee_name TEXT NOT NULL,
                callee_expr TEXT NOT NULL DEFAULT '',
                target_module TEXT NOT NULL DEFAULT '',
                target_qualified TEXT NOT NULL DEFAULT '',
                receiver TEXT NOT NULL DEFAULT '',
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                resolution_status TEXT NOT NULL DEFAULT 'unresolved',
                confidence REAL NOT NULL DEFAULT 0.0
            );
            CREATE INDEX IF NOT EXISTS calls_target ON calls(target_symbol_id);
            CREATE INDEX IF NOT EXISTS calls_name ON calls(callee_name COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS text_chunks (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chunks_file ON text_chunks(file_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS text_chunks_fts USING fts5(
                content, content='text_chunks', content_rowid='id', tokenize='unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS text_chunks_ai AFTER INSERT ON text_chunks BEGIN
                INSERT INTO text_chunks_fts(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS text_chunks_ad AFTER DELETE ON text_chunks BEGIN
                INSERT INTO text_chunks_fts(text_chunks_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS text_chunks_au AFTER UPDATE ON text_chunks BEGIN
                INSERT INTO text_chunks_fts(text_chunks_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
                INSERT INTO text_chunks_fts(rowid, content) VALUES (new.id, new.content);
            END;
        """)
        if version != SCHEMA_VERSION:
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()

    @staticmethod
    def _root(root: Union[str, Path]) -> Path:
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise ValueError(f"项目目录不存在：{resolved}")
        return resolved

    @staticmethod
    def _project_id(conn: sqlite3.Connection, root: Path, create: bool = False) -> Optional[int]:
        row = conn.execute("SELECT id FROM projects WHERE root=?", (str(root),)).fetchone()
        if row:
            return int(row[0])
        if not create:
            return None
        cur = conn.execute(
            "INSERT INTO projects(root,last_indexed,generation,schema_version) VALUES (?,?,?,?)",
            (str(root), 0.0, 0, SCHEMA_VERSION),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    @staticmethod
    def _iter_files(root: Path) -> Iterator[Tuple[Path, str, str]]:
        matcher = _IgnoreMatcher(root)
        for dirpath, dirnames, filenames in os.walk(str(root)):
            current = Path(dirpath)
            kept_dirs: List[str] = []
            for dirname in sorted(dirnames):
                if dirname in _HARD_SKIP_DIRS:
                    continue
                # Do not prune ignored directories: a later ! rule may re-include children.
                # Hard skips above protect the common very large generated directories.
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for filename in sorted(filenames):
                path = current / filename
                rel = path.relative_to(root).as_posix()
                if matcher.ignored(rel, is_dir=False):
                    continue
                language = _language(path)
                if language is not None:
                    yield path, rel, language

    def index_project(self, root: Union[str, Path]) -> IndexStatus:
        project_root = self._root(root)
        started = time.perf_counter()
        added = updated = reused = confirmed = removed = 0
        skipped: List[str] = []
        with self._lock, self._connect() as conn:
            self._ensure_schema(conn)
            project_id = self._project_id(conn, project_root, create=True)
            assert project_id is not None
            existing = {
                row["path"]: dict(row)
                for row in conn.execute("SELECT * FROM files WHERE project_id=?", (project_id,))
            }
            seen: set = set()
            for path, rel, language in self._iter_files(project_root):
                try:
                    stat = path.stat()
                except OSError:
                    skipped.append(rel)
                    continue
                if stat.st_size > MAX_INDEX_FILE_BYTES:
                    skipped.append(rel)
                    continue
                old = existing.get(rel)
                if old and int(old["size"]) == stat.st_size and int(old["mtime_ns"]) == stat.st_mtime_ns:
                    seen.add(rel)
                    reused += 1
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    skipped.append(rel)
                    continue
                source = _decode(data)
                if source is None:
                    skipped.append(rel)
                    continue
                seen.add(rel)
                source_hash = hashlib.sha256(data).hexdigest()
                if old and old["source_hash"] == source_hash:
                    conn.execute(
                        "UPDATE files SET size=?,mtime_ns=?,language=? WHERE id=?",
                        (stat.st_size, stat.st_mtime_ns, language, old["id"]),
                    )
                    confirmed += 1
                    continue

                symbols: List[Dict[str, Any]] = []
                calls: List[Dict[str, Any]] = []
                imports: List[Dict[str, Any]] = []
                parse_error = ""
                module_name = _module_name(rel) if language == "python" else ""
                if language == "python":
                    symbols, calls, imports, parse_error = _extract_python(
                        source, module_name, path.name == "__init__.py"
                    )
                now = time.time()
                if old:
                    file_id = int(old["id"])
                    conn.execute("DELETE FROM text_chunks WHERE file_id=?", (file_id,))
                    conn.execute("DELETE FROM calls WHERE file_id=?", (file_id,))
                    conn.execute("DELETE FROM imports WHERE file_id=?", (file_id,))
                    conn.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
                    conn.execute(
                        """UPDATE files SET module_name=?,language=?,size=?,mtime_ns=?,source_hash=?,
                           parse_error=?,indexed_at=?,line_count=? WHERE id=?""",
                        (module_name, language, stat.st_size, stat.st_mtime_ns, source_hash, parse_error,
                         now, len(source.splitlines()), file_id),
                    )
                    updated += 1
                else:
                    cur = conn.execute(
                        """INSERT INTO files(project_id,path,module_name,language,size,mtime_ns,source_hash,
                           parse_error,indexed_at,line_count) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (project_id, rel, module_name, language, stat.st_size, stat.st_mtime_ns,
                          source_hash, parse_error, now, len(source.splitlines())),
                    )
                    assert cur.lastrowid is not None
                    file_id = cur.lastrowid
                    added += 1
                symbol_ids: Dict[str, int] = {}
                for symbol in symbols:
                    cur = conn.execute(
                        """INSERT INTO symbols(file_id,name,qualified_name,symbol_key,kind,start_line,end_line,
                           signature,doc,parent,bases) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (file_id, symbol["name"], symbol["qualified_name"], symbol["symbol_key"], symbol["kind"],
                          symbol["start_line"], symbol["end_line"], symbol["signature"],
                          symbol["doc"], symbol["parent"], json.dumps(symbol["bases"])),
                    )
                    assert cur.lastrowid is not None
                    symbol_ids[symbol["qualified_name"]] = cur.lastrowid
                for call in calls:
                    conn.execute(
                        """INSERT INTO calls(file_id,caller_symbol_id,target_symbol_id,callee_name,
                           callee_expr,target_module,target_qualified,receiver,start_line,end_line)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (file_id, symbol_ids.get(call["caller_qualified"]), None,
                          call["callee_name"], call["callee_expr"], call["target_module"],
                          call["target_qualified"], call["receiver"], call["start_line"], call["end_line"]),
                    )
                for binding in imports:
                    conn.execute(
                        """INSERT INTO imports(file_id,binding_scope,module,imported_name,local_name,
                           level,is_star,start_line,end_line) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (file_id, binding["binding_scope"], binding["module"],
                         binding["imported_name"], binding["local_name"], binding["level"],
                         binding["is_star"], binding["start_line"], binding["end_line"]),
                    )
                for start_line, end_line, content, content_hash in _chunks(source):
                    conn.execute(
                        "INSERT INTO text_chunks(file_id,start_line,end_line,content,content_hash) VALUES (?,?,?,?,?)",
                        (file_id, start_line, end_line, content, content_hash),
                    )

            for rel, row in existing.items():
                if rel not in seen:
                    conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
                    removed += 1

            self._resolve_calls(conn, project_id)
            changed = added + updated + removed
            conn.execute(
                """UPDATE projects SET last_indexed=?,generation=generation+?,schema_version=?
                   WHERE id=?""",
                (time.time(), int(changed > 0), SCHEMA_VERSION, project_id),
            )
            conn.commit()
            error_rows = conn.execute(
                "SELECT path,parse_error FROM files WHERE project_id=? AND parse_error<>''",
                (project_id,),
            ).fetchall()
            parse_errors = {str(row["path"]): str(row["parse_error"]) for row in error_rows}
            language_rows = conn.execute(
                "SELECT language,COUNT(*) count FROM files WHERE project_id=? GROUP BY language",
                (project_id,),
            ).fetchall()
            languages = {str(row["language"]): int(row["count"]) for row in language_rows}
        return IndexStatus(
            root=str(project_root), project_id=project_id,
            indexed_files=sum(languages.values()), added_files=added,
            updated_files=updated, reused_files=reused, hash_confirmed_files=confirmed,
            removed_files=removed, skipped_files=skipped, parse_errors=parse_errors,
            languages=languages, duration_ms=int((time.perf_counter() - started) * 1000),
            updated_at=time.time(),
        )

    @staticmethod
    def _resolve_calls(conn: sqlite3.Connection, project_id: int) -> None:
        conn.execute(
            """UPDATE calls SET target_symbol_id=NULL,resolution_status='unresolved',confidence=0
               WHERE file_id IN
               (SELECT id FROM files WHERE project_id=?)""", (project_id,)
        )
        calls = conn.execute(
            """SELECT c.*,f.module_name,cs.qualified_name caller_qualified
               FROM calls c
               JOIN files f ON f.id=c.file_id
               LEFT JOIN symbols cs ON cs.id=c.caller_symbol_id
               WHERE f.project_id=?""", (project_id,)
        ).fetchall()
        symbol_rows = conn.execute(
            """SELECT s.*,f.module_name,f.path FROM symbols s
               JOIN files f ON f.id=s.file_id WHERE f.project_id=? ORDER BY s.id""",
            (project_id,),
        ).fetchall()
        by_name: Dict[str, List[sqlite3.Row]] = {}
        by_module_name: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
        by_module_qualified: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
        for row in symbol_rows:
            name_key = str(row["name"]).casefold()
            module_key = str(row["module_name"]).casefold()
            by_name.setdefault(name_key, []).append(row)
            by_module_name.setdefault((module_key, name_key), []).append(row)
            by_module_qualified.setdefault(
                (module_key, str(row["qualified_name"]).casefold()), []
            ).append(row)

        bindings: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
        for row in conn.execute(
            """SELECT i.*,f.module_name source_module FROM imports i
               JOIN files f ON f.id=i.file_id
               WHERE f.project_id=? AND i.binding_scope='' AND i.is_star=0""",
            (project_id,),
        ):
            bindings.setdefault(
                (str(row["source_module"]).casefold(), str(row["local_name"]).casefold()), []
            ).append(row)

        def unique(rows: Iterable[sqlite3.Row]) -> List[sqlite3.Row]:
            result: List[sqlite3.Row] = []
            seen_ids: set = set()
            for row in rows:
                row_id = int(row["id"])
                if row_id not in seen_ids:
                    seen_ids.add(row_id)
                    result.append(row)
            return result

        def exported(module: str, name: str, seen: Optional[set] = None) -> List[sqlite3.Row]:
            key = (module.casefold(), name.casefold())
            exact = by_module_name.get(key, [])
            top_level = [row for row in exact if row["qualified_name"] == row["name"]]
            if top_level:
                return unique(top_level)
            visited = set(seen or ())
            if key in visited:
                return []
            visited.add(key)
            resolved: List[sqlite3.Row] = []
            for binding in bindings.get(key, []):
                target_module = str(binding["module"])
                target_name = str(binding["imported_name"] or name)
                resolved.extend(exported(target_module, target_name, visited))
            return unique(resolved)

        def lexical(call: sqlite3.Row) -> List[sqlite3.Row]:
            module = str(call["module_name"])
            name = str(call["callee_name"])
            caller = str(call["caller_qualified"] or "")
            parts = caller.split(".") if caller else []
            candidates: List[sqlite3.Row] = []
            while parts:
                qualified = ".".join(parts + [name]).casefold()
                candidates = by_module_qualified.get((module.casefold(), qualified), [])
                if candidates:
                    return unique(candidates)
                parts.pop()
            return unique(by_module_qualified.get((module.casefold(), name.casefold()), []))

        def super_target(call: sqlite3.Row, marker: str) -> List[sqlite3.Row]:
            qualified = marker[len("super:"):]
            class_qualified, _, method_name = qualified.rpartition(".")
            module = str(call["module_name"])
            classes = by_module_qualified.get((module.casefold(), class_qualified.casefold()), [])
            targets: List[sqlite3.Row] = []
            for class_row in classes:
                try:
                    bases = json.loads(str(class_row["bases"] or "[]"))
                except (TypeError, ValueError):
                    bases = []
                for base in bases:
                    base_name = str(base).split(".")[-1]
                    base_rows = exported(module, base_name)
                    if not base_rows:
                        for binding in bindings.get((module.casefold(), base_name.casefold()), []):
                            base_rows.extend(exported(str(binding["module"]), str(binding["imported_name"])))
                    for base_row in unique(base_rows):
                        base_module = str(base_row["module_name"])
                        base_qname = str(base_row["qualified_name"])
                        targets.extend(by_module_qualified.get(
                            (base_module.casefold(), f"{base_qname}.{method_name}".casefold()), []
                        ))
            return unique(targets)

        for call in calls:
            target_module = str(call["target_module"] or "")
            target_qualified = str(call["target_qualified"] or "")
            name = str(call["callee_name"])
            confidence = 0.0
            choices: List[sqlite3.Row] = []
            if target_qualified.startswith("super:"):
                choices = super_target(call, target_qualified)
                confidence = 0.95
            elif target_qualified:
                choices = by_module_qualified.get(
                    (target_module.casefold(), target_qualified.casefold()), []
                )
                confidence = 1.0
                if not choices:
                    choices = super_target(call, "super:" + target_qualified)
                    confidence = 0.9
            elif target_module:
                choices = exported(target_module, name)
                confidence = 1.0 if target_module != str(call["module_name"]) else 0.9
                if not choices and target_module == str(call["module_name"]):
                    choices = lexical(call)
            if not choices and not target_module:
                all_choices = by_name.get(name.casefold(), [])
                if len(all_choices) == 1:
                    choices = all_choices
                    confidence = 0.45
                elif len(all_choices) > 1:
                    conn.execute(
                        "UPDATE calls SET resolution_status='ambiguous' WHERE id=?", (call["id"],)
                    )
                    continue
            choices = unique(choices)
            if len(choices) == 1:
                conn.execute(
                    """UPDATE calls SET target_symbol_id=?,resolution_status='resolved',confidence=?
                       WHERE id=?""",
                    (choices[0]["id"], confidence, call["id"]),
                )
            elif len(choices) > 1:
                conn.execute(
                    "UPDATE calls SET resolution_status='ambiguous' WHERE id=?", (call["id"],)
                )

    def _lookup_project(self, conn: sqlite3.Connection, root: Union[str, Path]) -> Tuple[Path, Optional[int]]:
        resolved = self._root(root)
        return resolved, self._project_id(conn, resolved, create=False)

    def project_revision(self, root: Union[str, Path]) -> int:
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return 0
            row = conn.execute("SELECT generation FROM projects WHERE id=?", (project_id,)).fetchone()
            return int(row[0]) if row else 0

    def refresh_paths(self, root: Union[str, Path], paths: Iterable[str]) -> IndexStatus:
        """Force a bounded set of stale files through hashing/parsing, then resolve edges."""
        normalized: List[str] = []
        project_root = self._root(root)
        for raw in paths:
            value = str(raw).replace("\\", "/")
            try:
                candidate = (project_root / value).resolve()
                candidate.relative_to(project_root)
            except (OSError, RuntimeError, ValueError):
                continue
            if candidate.is_file() and value not in normalized:
                normalized.append(value)
        if normalized:
            with self._lock, self._connect() as conn:
                project_id = self._project_id(conn, project_root, create=False)
                if project_id is not None:
                    placeholders = ",".join("?" for _ in normalized)
                    conn.execute(
                        f"UPDATE files SET mtime_ns=-1 WHERE project_id=? AND path IN ({placeholders})",
                        [project_id, *normalized],
                    )
                    conn.commit()
        return self.index_project(project_root)

    def parse_warnings(self, root: Union[str, Path]) -> List[str]:
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            rows = conn.execute(
                """SELECT path,parse_error FROM files WHERE project_id=? AND parse_error<>''
                   ORDER BY path""",
                (project_id,),
            ).fetchall()
            return [f"{row['path']}: {row['parse_error']}" for row in rows]

    def resolve_symbol_rows(
        self,
        root: Union[str, Path],
        path: str = "",
        line: Optional[int] = None,
        expression: str = "",
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        normalized_path = path.replace("\\", "/")
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            if normalized_path and line is not None:
                rows = conn.execute(
                    """SELECT s.*,f.path,f.module_name,f.language,f.source_hash
                       FROM symbols s JOIN files f ON f.id=s.file_id
                       WHERE f.project_id=? AND f.path=? AND s.start_line<=? AND s.end_line>=?
                       ORDER BY (s.end_line-s.start_line),s.start_line DESC LIMIT ?""",
                    (project_id, normalized_path, line, line, max(1, limit)),
                ).fetchall()
                if rows:
                    return [dict(row) for row in rows]
            identifiers = re.findall(r"[A-Za-z_]\w*", expression)
            if not identifiers:
                return []
            name = identifiers[-1]
            if normalized_path:
                imported = conn.execute(
                    """SELECT i.module,i.imported_name FROM imports i
                       JOIN files f ON f.id=i.file_id
                       WHERE f.project_id=? AND f.path=? AND i.local_name=? COLLATE NOCASE
                       ORDER BY CASE WHEN i.binding_scope='' THEN 0 ELSE 1 END,i.start_line LIMIT 1""",
                    (project_id, normalized_path, identifiers[0]),
                ).fetchone()
                if imported:
                    module = str(imported["module"])
                    imported_name = str(imported["imported_name"] or name)
                    rows = conn.execute(
                        """SELECT s.*,f.path,f.module_name,f.language,f.source_hash
                           FROM symbols s JOIN files f ON f.id=s.file_id
                           WHERE f.project_id=? AND f.module_name=? COLLATE NOCASE
                             AND s.name=? COLLATE NOCASE
                           ORDER BY CASE WHEN s.qualified_name=s.name THEN 0 ELSE 1 END,s.id LIMIT ?""",
                        (project_id, module, imported_name, max(1, limit)),
                    ).fetchall()
                    if rows:
                        return [dict(row) for row in rows]
            rows = conn.execute(
                """SELECT s.*,f.path,f.module_name,f.language,f.source_hash
                   FROM symbols s JOIN files f ON f.id=s.file_id
                   WHERE f.project_id=? AND s.name=? COLLATE NOCASE
                   ORDER BY CASE WHEN f.path=? THEN 0 ELSE 1 END,s.id LIMIT ?""",
                (project_id, name, normalized_path, max(1, limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def symbol_by_id(self, root: Union[str, Path], symbol_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return None
            row = conn.execute(
                """SELECT s.*,f.path,f.module_name,f.language,f.source_hash
                   FROM symbols s JOIN files f ON f.id=s.file_id
                   WHERE f.project_id=? AND s.id=?""",
                (project_id, symbol_id),
            ).fetchone()
            return dict(row) if row else None

    def reference_rows(self, root: Union[str, Path], symbol_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            rows = conn.execute(
                """SELECT c.*,f.path,f.module_name,f.language,f.source_hash,
                          cs.symbol_key caller_symbol_key,cs.qualified_name caller_qualified
                   FROM calls c JOIN files f ON f.id=c.file_id
                   LEFT JOIN symbols cs ON cs.id=c.caller_symbol_id
                   WHERE f.project_id=? AND c.target_symbol_id=?
                   ORDER BY f.path,c.start_line LIMIT ?""",
                (project_id, symbol_id, max(1, limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def trace_rows(
        self,
        root: Union[str, Path],
        symbol_id: int,
        direction: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if direction not in {"callers", "callees"}:
            raise ValueError("direction must be callers or callees")
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            if direction == "callers":
                join_condition = "c.target_symbol_id=?"
                symbol_join = "s.id=c.caller_symbol_id"
            else:
                join_condition = "c.caller_symbol_id=?"
                symbol_join = "s.id=c.target_symbol_id"
            rows = conn.execute(
                f"""SELECT DISTINCT s.*,f.path,f.module_name,f.language,f.source_hash,
                            c.resolution_status,c.confidence
                     FROM calls c JOIN symbols s ON {symbol_join}
                     JOIN files f ON f.id=s.file_id
                     WHERE f.project_id=? AND {join_condition}
                     ORDER BY c.confidence DESC,f.path,s.start_line LIMIT ?""",
                (project_id, symbol_id, max(1, limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def repository_map(
        self,
        root: Union[str, Path],
        current_file: str = "",
        max_tokens: int = 900,
    ) -> str:
        char_budget = max(400, max_tokens * 4)
        current = current_file.replace("\\", "/")
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return ""
            rows = conn.execute(
                """SELECT f.path,f.module_name,s.kind,s.qualified_name,s.signature
                   FROM files f LEFT JOIN symbols s ON s.file_id=f.id
                   WHERE f.project_id=?
                   ORDER BY CASE WHEN f.path=? THEN 0 ELSE 1 END,f.path,
                            CASE s.kind WHEN 'class' THEN 0 WHEN 'function' THEN 1 ELSE 2 END,
                            s.start_line""",
                (project_id, current),
            ).fetchall()
            imports = conn.execute(
                """SELECT DISTINCT f.path,i.module FROM imports i JOIN files f ON f.id=i.file_id
                   WHERE f.project_id=? AND i.binding_scope='' ORDER BY f.path,i.module""",
                (project_id,),
            ).fetchall()
        import_map: Dict[str, List[str]] = {}
        for row in imports:
            module = str(row["module"])
            if module:
                import_map.setdefault(str(row["path"]), []).append(module)
        output: List[str] = []
        last_path = ""
        used = 0
        for row in rows:
            path_value = str(row["path"])
            if path_value != last_path:
                edge_text = ", ".join(import_map.get(path_value, [])[:8])
                line_text = path_value + (f" -> {edge_text}" if edge_text else "")
                last_path = path_value
                if used + len(line_text) + 1 > char_budget:
                    output.append("… repository map truncated")
                    break
                output.append(line_text)
                used += len(line_text) + 1
            if row["qualified_name"]:
                signature = str(row["signature"] or row["qualified_name"]).strip()
                line_text = f"  {row['kind']} {row['qualified_name']}: {signature}"
            else:
                continue
            if used + len(line_text) + 1 > char_budget:
                output.append("… repository map truncated")
                break
            output.append(line_text)
            used += len(line_text) + 1
        return "\n".join(output)

    def list_files(self, root: Union[str, Path]) -> List[str]:
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            return [str(row[0]) for row in conn.execute(
                "SELECT path FROM files WHERE project_id=? ORDER BY path COLLATE NOCASE", (project_id,)
            )]

    def file_record(self, root: Union[str, Path], path: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return None
            row = conn.execute(
                "SELECT * FROM files WHERE project_id=? AND path=?", (project_id, path.replace("\\", "/"))
            ).fetchone()
            return dict(row) if row else None

    def symbol_rows(self, root: Union[str, Path], name: str,
                    current_file: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            rows = conn.execute(
                """SELECT s.*,f.path,f.language,f.source_hash FROM symbols s
                   JOIN files f ON f.id=s.file_id
                   WHERE f.project_id=? AND s.name=? COLLATE NOCASE
                   ORDER BY CASE WHEN f.path=? THEN 0 ELSE 1 END,s.id LIMIT ?""",
                (project_id, name, (current_file or "").replace("\\", "/"), max(1, limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def search_symbol_rows(self, root: Union[str, Path], query: str,
                           current_file: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        needle = query.strip()
        if not needle:
            return []
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            rows = conn.execute(
                """SELECT s.*,f.path,f.language,f.source_hash FROM symbols s
                   JOIN files f ON f.id=s.file_id WHERE f.project_id=? AND s.name LIKE ? ESCAPE '\\'
                   ORDER BY CASE WHEN s.name=? COLLATE NOCASE THEN 0
                                 WHEN s.name LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END,
                            CASE WHEN f.path=? THEN 0 ELSE 1 END,LENGTH(s.name),s.id LIMIT ?""",
                (project_id, "%" + _escape_like(needle) + "%", needle,
                 _escape_like(needle) + "%", (current_file or "").replace("\\", "/"), max(1, limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def search_file_rows(self, root: Union[str, Path], query: str, limit: int = 20) -> List[Dict[str, Any]]:
        needle = query.strip()
        if not needle:
            return []
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            rows = conn.execute(
                """SELECT * FROM files WHERE project_id=? AND path LIKE ? ESCAPE '\\'
                   ORDER BY CASE WHEN path LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END,
                            LENGTH(path),path COLLATE NOCASE LIMIT ?""",
                (project_id, "%" + _escape_like(needle) + "%",
                 _escape_like(needle) + "%", max(1, limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def call_rows(self, root: Union[str, Path], symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            rows = conn.execute(
                """SELECT c.*,f.path,f.language,f.source_hash,ts.name target_name,
                          cs.name caller_name,cs.start_line caller_start_line,cs.end_line caller_end_line
                   FROM calls c JOIN files f ON f.id=c.file_id
                   LEFT JOIN symbols ts ON ts.id=c.target_symbol_id
                   LEFT JOIN symbols cs ON cs.id=c.caller_symbol_id
                   WHERE f.project_id=? AND (c.callee_name=? COLLATE NOCASE OR ts.name=? COLLATE NOCASE)
                   ORDER BY f.path,c.start_line LIMIT ?""",
                (project_id, symbol, symbol, max(1, limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def callee_rows(self, root: Union[str, Path], symbol: str, limit: int = 30) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            rows = conn.execute(
                """SELECT DISTINCT ts.*,tf.path,tf.language,tf.source_hash
                   FROM symbols caller JOIN files cf ON cf.id=caller.file_id
                   JOIN calls c ON c.caller_symbol_id=caller.id
                   JOIN symbols ts ON ts.id=c.target_symbol_id JOIN files tf ON tf.id=ts.file_id
                   WHERE cf.project_id=? AND caller.name=? COLLATE NOCASE
                   ORDER BY ts.id LIMIT ?""",
                (project_id, symbol, max(1, limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def caller_rows(self, root: Union[str, Path], symbol: str, limit: int = 30) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            rows = conn.execute(
                """SELECT DISTINCT cs.*,cf.path,cf.language,cf.source_hash
                   FROM symbols target JOIN files tf ON tf.id=target.file_id
                   JOIN calls c ON c.target_symbol_id=target.id
                   JOIN symbols cs ON cs.id=c.caller_symbol_id JOIN files cf ON cf.id=cs.file_id
                   WHERE tf.project_id=? AND target.name=? COLLATE NOCASE
                   ORDER BY cs.id LIMIT ?""",
                (project_id, symbol, max(1, limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def search_text(self, root: Union[str, Path], query: str, limit: int = 20) -> List[Dict[str, Any]]:
        match = _fts_query(query)
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            rows: List[sqlite3.Row] = []
            if match:
                try:
                    rows = list(conn.execute(
                        """SELECT tc.id,tc.start_line,tc.end_line,tc.content,tc.content_hash,
                                  f.path,f.language,f.source_hash,bm25(text_chunks_fts) bm25_score
                           FROM text_chunks_fts JOIN text_chunks tc ON tc.id=text_chunks_fts.rowid
                           JOIN files f ON f.id=tc.file_id
                           WHERE text_chunks_fts MATCH ? AND f.project_id=?
                           ORDER BY bm25_score LIMIT ?""",
                        (match, project_id, max(1, limit)),
                    ).fetchall())
                except sqlite3.OperationalError:
                    rows = []
            # unicode61 does not segment Chinese natural-language questions.  A
            # bounded literal fallback keeps identifiers and CJK phrases useful
            # without forcing every word to occur in the same chunk.
            if len(rows) < max(1, limit):
                literals = _literal_terms(query)
                if literals:
                    clauses = " OR ".join("tc.content LIKE ? ESCAPE '\\'" for _ in literals)
                    params: List[Any] = ["%" + _escape_like(term) + "%" for term in literals]
                    params.extend([project_id, max(1, limit)])
                    literal_rows = conn.execute(
                        f"""SELECT tc.id,tc.start_line,tc.end_line,tc.content,tc.content_hash,
                                   f.path,f.language,f.source_hash,1000.0 bm25_score
                            FROM text_chunks tc JOIN files f ON f.id=tc.file_id
                            WHERE ({clauses}) AND f.project_id=?
                            ORDER BY f.path,tc.start_line LIMIT ?""",
                        params,
                    ).fetchall()
                    seen = {int(row["id"]) for row in rows}
                    rows.extend(row for row in literal_rows if int(row["id"]) not in seen)
            return [dict(row) for row in rows[:max(1, limit)]]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_query(value: str) -> str:
    tokens = re.findall(r"[^\W_]+(?:_[^\W_]+)*", value, flags=re.UNICODE)
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens[:20])


def _literal_terms(value: str) -> List[str]:
    identifiers = re.findall(r"[A-Za-z_]\w*", value)
    cjk = re.findall(r"[\u3400-\u9fff]{2,}", value)
    terms: List[str] = []
    for term in identifiers + cjk:
        if term not in terms:
            terms.append(term)
    return terms[:8]


__all__ = ["CodeIndex", "IndexStatus", "SCHEMA_VERSION"]
