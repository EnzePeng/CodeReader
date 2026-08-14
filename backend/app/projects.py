"""Opaque project capabilities and project-rooted path resolution.

The browser never receives an absolute filesystem root after a project has
been opened.  It receives a random ``project_id`` and all subsequent file
lookups are resolved relative to the registered root.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, Optional


class ProjectError(Exception):
    """Base class for project capability failures."""

    code = "project_error"
    status_code = 400


class ProjectNotFound(ProjectError):
    code = "project_not_found"
    status_code = 404


class InvalidProjectRoot(ProjectError):
    code = "invalid_project_root"
    status_code = 400


class InvalidProjectPath(ProjectError):
    code = "invalid_project_path"
    status_code = 400


@dataclass(frozen=True)
class Project:
    project_id: str
    root: Path

    @property
    def name(self) -> str:
        return self.root.name or self.root.anchor


@dataclass(frozen=True)
class ResolvedProjectPath:
    project: Project
    path: Path
    relative_path: str


class ProjectRegistry:
    """Process-local registry of opaque project capabilities."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._projects: Dict[str, Project] = {}
        self._ids_by_root: Dict[str, str] = {}

    @staticmethod
    def _root_key(root: Path) -> str:
        return str(root).casefold()

    def open(self, absolute_path: str) -> Project:
        if not absolute_path or "\x00" in absolute_path:
            raise InvalidProjectRoot("项目目录不能为空")
        requested = Path(absolute_path)
        if not requested.is_absolute():
            raise InvalidProjectRoot("项目目录必须是绝对路径")
        try:
            root = requested.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InvalidProjectRoot("项目目录不存在或无法访问") from exc
        if not root.is_dir():
            raise InvalidProjectRoot("项目路径不是目录")

        key = self._root_key(root)
        with self._lock:
            known_id = self._ids_by_root.get(key)
            if known_id is not None:
                return self._projects[known_id]
            while True:
                project_id = secrets.token_urlsafe(24)
                if project_id not in self._projects:
                    break
            project = Project(project_id=project_id, root=root)
            self._projects[project_id] = project
            self._ids_by_root[key] = project_id
            return project

    def get(self, project_id: str) -> Project:
        with self._lock:
            project = self._projects.get(project_id)
        if project is None:
            raise ProjectNotFound("项目句柄不存在或已失效")
        return project

    @staticmethod
    def _validate_relative(relative_path: str) -> str:
        if not relative_path or "\x00" in relative_path:
            raise InvalidProjectPath("项目内路径不能为空")

        # Check both path grammars.  On Windows ``Path`` catches drive/UNC
        # paths; checking PurePosixPath as well keeps the invariant portable.
        win = PureWindowsPath(relative_path)
        posix = PurePosixPath(relative_path.replace("\\", "/"))
        if win.is_absolute() or win.drive or win.root or posix.is_absolute():
            raise InvalidProjectPath("只允许项目内相对路径")
        if any(part == ".." for part in win.parts) or any(part == ".." for part in posix.parts):
            raise InvalidProjectPath("项目内路径不能包含 ..")
        if any(part in ("", ".") for part in posix.parts):
            # Repeated separators are harmless after normalization; an
            # explicit current-directory segment is rejected to keep one
            # canonical wire representation per file.
            if "/./" in f"/{relative_path.replace(chr(92), '/')}/":
                raise InvalidProjectPath("项目内路径必须是规范相对路径")
        return "/".join(posix.parts)

    def resolve_path(
        self,
        project_id: str,
        relative_path: str,
        *,
        must_exist: bool = True,
        file_only: bool = False,
        directory_only: bool = False,
    ) -> ResolvedProjectPath:
        project = self.get(project_id)
        normalized = self._validate_relative(relative_path)
        try:
            candidate = (project.root / Path(*PurePosixPath(normalized).parts)).resolve(
                strict=must_exist)
            candidate.relative_to(project.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise InvalidProjectPath("路径不存在或不在已打开的项目内") from exc
        if file_only and not candidate.is_file():
            raise InvalidProjectPath("项目内文件不存在")
        if directory_only and not candidate.is_dir():
            raise InvalidProjectPath("项目内目录不存在")
        canonical = candidate.relative_to(project.root).as_posix()
        return ResolvedProjectPath(project=project, path=candidate, relative_path=canonical)

    def resolve_file(self, project_id: str, relative_path: str) -> ResolvedProjectPath:
        return self.resolve_path(project_id, relative_path, file_only=True)

    def resolve_directory(
        self, project_id: str, relative_path: Optional[str] = None
    ) -> ResolvedProjectPath:
        rel = relative_path or "."
        if rel == ".":
            project = self.get(project_id)
            return ResolvedProjectPath(project, project.root, "")
        return self.resolve_path(project_id, rel, directory_only=True)


registry = ProjectRegistry()
