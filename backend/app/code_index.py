"""Persistent, incremental, Python-first evidence index.

The index combines compiler-like Python facts with FTS5 text coverage.  Files that
cannot be parsed (including syntax newer than the running interpreter) are still
chunked and searchable, and their parse failure is exposed through ``IndexStatus``.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple, Union

SCHEMA_VERSION = 1
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
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return list(reversed(parts))


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, source: str, aliases: Dict[str, str]) -> None:
        self.lines = source.splitlines()
        self.aliases = aliases
        self.stack: List[Tuple[str, str]] = []
        self.symbols: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []

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
            "kind": kind,
            "start_line": start,
            "end_line": end,
            "signature": signature[:500],
            "doc": doc,
            "parent": self.stack[-1][0] if self.stack else "",
        })
        return qualified

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add_symbol(node, node.name, "class")
        self.stack.append((node.name, "class"))
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node: ast.AST) -> None:
        name = str(getattr(node, "name"))
        kind = "method" if self.stack and self.stack[-1][1] == "class" else "function"
        self._add_symbol(node, name, kind)
        self.stack.append((name, kind))
        self.generic_visit(node)
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

    def visit_Call(self, node: ast.Call) -> None:
        parts = _call_parts(node.func)
        if parts:
            raw_name = parts[-1]
            target_name = self.aliases.get(raw_name, raw_name) if len(parts) == 1 else raw_name
            self.calls.append({
                "caller_qualified": ".".join(item[0] for item in self.stack),
                "callee_name": target_name,
                "receiver": ".".join(parts[:-1]),
                "start_line": int(getattr(node, "lineno", 1)),
                "end_line": node.end_lineno or node.lineno,
            })
        self.generic_visit(node)


def _extract_python(source: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    try:
        # On Python 3.13 this naturally accepts 3.13 nodes.  Older runtimes report a
        # parse error, after which the file remains fully available through FTS.
        tree = ast.parse(source, type_comments=True)
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        message = f"{exc.__class__.__name__}: {exc}"
        return [], [], message[:1000]
    aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name.split(".")[-1]
    visitor = _PythonVisitor(source, aliases)
    visitor.visit(tree)
    return visitor.symbols, visitor.calls, ""


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
        with self._connect() as conn:
            self._ensure_schema(conn)

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
                schema_version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                path TEXT NOT NULL,
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
                kind TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                signature TEXT NOT NULL DEFAULT '',
                doc TEXT NOT NULL DEFAULT '',
                parent TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS symbols_file ON symbols(file_id);
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                caller_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
                target_symbol_id INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
                callee_name TEXT NOT NULL,
                receiver TEXT NOT NULL DEFAULT '',
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL
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
            "INSERT INTO projects(root,last_indexed,schema_version) VALUES (?,?,?)",
            (str(root), 0.0, SCHEMA_VERSION),
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
                parse_error = ""
                if language == "python":
                    symbols, calls, parse_error = _extract_python(source)
                now = time.time()
                if old:
                    file_id = int(old["id"])
                    conn.execute("DELETE FROM text_chunks WHERE file_id=?", (file_id,))
                    conn.execute("DELETE FROM calls WHERE file_id=?", (file_id,))
                    conn.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
                    conn.execute(
                        """UPDATE files SET language=?,size=?,mtime_ns=?,source_hash=?,
                           parse_error=?,indexed_at=?,line_count=? WHERE id=?""",
                        (language, stat.st_size, stat.st_mtime_ns, source_hash, parse_error,
                         now, len(source.splitlines()), file_id),
                    )
                    updated += 1
                else:
                    cur = conn.execute(
                        """INSERT INTO files(project_id,path,language,size,mtime_ns,source_hash,
                           parse_error,indexed_at,line_count) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (project_id, rel, language, stat.st_size, stat.st_mtime_ns,
                         source_hash, parse_error, now, len(source.splitlines())),
                    )
                    assert cur.lastrowid is not None
                    file_id = cur.lastrowid
                    added += 1
                symbol_ids: Dict[str, int] = {}
                for symbol in symbols:
                    cur = conn.execute(
                        """INSERT INTO symbols(file_id,name,qualified_name,kind,start_line,end_line,
                           signature,doc,parent) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (file_id, symbol["name"], symbol["qualified_name"], symbol["kind"],
                         symbol["start_line"], symbol["end_line"], symbol["signature"],
                         symbol["doc"], symbol["parent"]),
                    )
                    assert cur.lastrowid is not None
                    symbol_ids[symbol["qualified_name"]] = cur.lastrowid
                for call in calls:
                    conn.execute(
                        """INSERT INTO calls(file_id,caller_symbol_id,target_symbol_id,callee_name,
                           receiver,start_line,end_line) VALUES (?,?,?,?,?,?,?)""",
                        (file_id, symbol_ids.get(call["caller_qualified"]), None,
                         call["callee_name"], call["receiver"], call["start_line"], call["end_line"]),
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
            conn.execute(
                "UPDATE projects SET last_indexed=?,schema_version=? WHERE id=?",
                (time.time(), SCHEMA_VERSION, project_id),
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
            """UPDATE calls SET target_symbol_id=NULL WHERE file_id IN
               (SELECT id FROM files WHERE project_id=?)""", (project_id,)
        )
        calls = conn.execute(
            """SELECT c.id,c.file_id,c.callee_name FROM calls c
               JOIN files f ON f.id=c.file_id WHERE f.project_id=?""", (project_id,)
        ).fetchall()
        symbols: Dict[str, List[sqlite3.Row]] = {}
        for row in conn.execute(
            """SELECT s.id,s.file_id,s.name FROM symbols s JOIN files f ON f.id=s.file_id
               WHERE f.project_id=? ORDER BY s.id""", (project_id,)
        ):
            symbols.setdefault(str(row["name"]).casefold(), []).append(row)
        for call in calls:
            choices = symbols.get(str(call["callee_name"]).casefold(), [])
            if not choices:
                continue
            same_file = [row for row in choices if int(row["file_id"]) == int(call["file_id"])]
            target = (same_file or choices)[0]
            conn.execute("UPDATE calls SET target_symbol_id=? WHERE id=?", (target["id"], call["id"]))

    def _lookup_project(self, conn: sqlite3.Connection, root: Union[str, Path]) -> Tuple[Path, Optional[int]]:
        resolved = self._root(root)
        return resolved, self._project_id(conn, resolved, create=False)

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
        if not match:
            return []
        with self._connect() as conn:
            _, project_id = self._lookup_project(conn, root)
            if project_id is None:
                return []
            try:
                rows = conn.execute(
                    """SELECT tc.id,tc.start_line,tc.end_line,tc.content,tc.content_hash,
                              f.path,f.language,f.source_hash,bm25(text_chunks_fts) bm25_score
                       FROM text_chunks_fts JOIN text_chunks tc ON tc.id=text_chunks_fts.rowid
                       JOIN files f ON f.id=tc.file_id
                       WHERE text_chunks_fts MATCH ? AND f.project_id=?
                       ORDER BY bm25_score LIMIT ?""",
                    (match, project_id, max(1, limit)),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            return [dict(row) for row in rows]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_query(value: str) -> str:
    tokens = re.findall(r"[^\W_]+(?:_[^\W_]+)*", value, flags=re.UNICODE)
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens[:20])


__all__ = ["CodeIndex", "IndexStatus", "SCHEMA_VERSION"]
