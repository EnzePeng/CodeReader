"""HTTP API：目录浏览、文件读取、结构分析、流式解读、追问、导出。"""
import asyncio
import hashlib
import inspect
import json
import logging
import os
import string
import threading
import urllib.parse
from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from . import cache, explainer, llama_launcher, llm, project_index, segmenter
from .citations import CitationFilter, EvidenceCatalog
from .config import APP_VERSION, data_dir, get_config, model_id, resolve_path, update_config_file
from .context_packer import ContextPacker
from .diagnostics import diagnostics
from .exploration import ExplorationRequest, ReadOnlyExplorer
from .projects import registry as project_registry
from .schemas import (
    ChatRequest,
    ExplainRequest,
    ProjectOpenRequest,
    ProjectOpenResponse,
    StreamSequence,
    StreamType,
)

router = APIRouter()
logger = logging.getLogger(__name__)

SKIP_DIRS = segmenter.SKIP_DIRS

_INDEX_MANAGER_LOCK = threading.RLock()
_CODE_INDEX: Any = None
_INDEX_STATUS: Dict[str, Any] = {}
_INDEX_INFLIGHT: Dict[str, Future] = {}

_REPORT_LOCK = threading.RLock()
_REPORTS: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()
_REPORT_LIMIT = 128


def _diagnostic_id() -> str:
    return uuid4().hex[:12]


def _public_index_status(status: Any) -> Dict[str, Any]:
    payload = status.to_dict() if hasattr(status, "to_dict") else dict(status)
    # IndexStatus keeps these fields for the local database adapter; neither
    # the absolute root nor its integer row id belongs on the public wire.
    payload.pop("root", None)
    payload.pop("project_id", None)
    payload["state"] = "ready"
    payload["files_indexed"] = int(payload.get("indexed_files", 0))
    payload["files_total"] = int(payload.get("total_files", 0))
    return payload


def _get_code_index():
    global _CODE_INDEX
    with _INDEX_MANAGER_LOCK:
        if _CODE_INDEX is None:
            try:
                from .code_index import CodeIndex
            except ImportError as exc:
                raise HTTPException(status_code=501, detail={
                    "code": "retriever_unavailable",
                    "message": "项目检索组件尚未安装",
                }) from exc
            _CODE_INDEX = CodeIndex(data_dir() / "code-index.db")
        return _CODE_INDEX


def _index_project_sync(project: Any, *, force: bool = False):
    """Index once per project while concurrent callers share one Future."""
    with _INDEX_MANAGER_LOCK:
        flight = _INDEX_INFLIGHT.get(project.project_id)
        if flight is None:
            if not force and project.project_id in _INDEX_STATUS:
                return _INDEX_STATUS[project.project_id]
            flight = Future()
            _INDEX_INFLIGHT[project.project_id] = flight
            owner = True
        else:
            owner = False
    if not owner:
        return flight.result()

    try:
        status = _get_code_index().index_project(project.root)
        with _INDEX_MANAGER_LOCK:
            _INDEX_STATUS[project.project_id] = status
        diagnostics.record_index(
            duration_ms=float(status.duration_ms),
            files=int(status.indexed_files),
            failed=len(status.parse_errors),
        )
        flight.set_result(status)
        return status
    except BaseException as exc:
        flight.set_exception(exc)
        raise
    finally:
        with _INDEX_MANAGER_LOCK:
            if _INDEX_INFLIGHT.get(project.project_id) is flight:
                _INDEX_INFLIGHT.pop(project.project_id, None)


def _schedule_index(project: Any, *, force: bool = False) -> None:
    """Start indexing without delaying project/file display."""
    future = asyncio.get_running_loop().run_in_executor(
        None, lambda: _index_project_sync(project, force=force))

    def report_failure(done) -> None:
        try:
            done.result()
        except Exception:
            logger.exception("background index failed project_id=%s", project.project_id)

    future.add_done_callback(report_failure)


def _record_report(project_id: str, relative_path: str, source_hash: str,
                   overview: str, segments: Dict[str, Dict[str, str]]) -> None:
    key = (project_id, relative_path)
    with _REPORT_LOCK:
        previous = _REPORTS.pop(key, None)
        if previous and previous.get("source_hash") == source_hash:
            merged_segments = dict(previous.get("segments", {}))
        else:
            merged_segments = {}
        merged_segments.update(segments)
        _REPORTS[key] = {
            "source_hash": source_hash,
            "overview": overview,
            "segments": merged_segments,
        }
        while len(_REPORTS) > _REPORT_LIMIT:
            _REPORTS.popitem(last=False)


def _report_for(project_id: str, relative_path: str,
                source_hash: str) -> Optional[Dict[str, Any]]:
    key = (project_id, relative_path)
    with _REPORT_LOCK:
        report = _REPORTS.get(key)
        if report is None or report.get("source_hash") != source_hash:
            return None
        _REPORTS.move_to_end(key)
        return {
            "overview": report.get("overview", ""),
            "segments": dict(report.get("segments", {})),
        }


def _clear_report(project_id: str, relative_path: str) -> None:
    with _REPORT_LOCK:
        _REPORTS.pop((project_id, relative_path), None)


def _project_file(project_id: str, relative_path: str):
    """Resolve one public wire path through an opaque project capability."""
    result = project_registry.resolve_file(project_id, relative_path)
    max_bytes = int(get_config()["explain"]["max_file_bytes"])
    try:
        size = result.path.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=404, detail={
            "code": "project_file_unavailable",
            "message": "项目内文件不存在或无法访问",
        }) from exc
    if size > max_bytes:
        raise HTTPException(status_code=413, detail={
            "code": "file_too_large",
            "message": f"文件超过 {max_bytes // 1_000_000} MB，暂不支持",
            "details": {"max_bytes": max_bytes},
        })
    return result


def _project_root(project_id: str) -> str:
    return str(project_registry.get(project_id).root)


def _public_file(result) -> Dict[str, Any]:
    text, encoding, _ = read_text_smart(result.path)
    return {
        "project_id": result.project.project_id,
        "path": result.relative_path,
        "relative_path": result.relative_path,
        "name": result.path.name,
        "language": segmenter.language_for(result.path.suffix),
        "encoding": encoding,
        "line_count": len(text.splitlines()),
        "content": text,
    }


def _evidence_dict(item: Any, root: Path) -> Optional[Dict[str, Any]]:
    validate = getattr(item, "validate", None)
    if callable(validate) and not validate(root):
        return None
    if hasattr(item, "to_dict"):
        value = item.to_dict()
    elif hasattr(item, "model_dump"):
        value = item.model_dump()
    elif isinstance(item, dict):
        value = dict(item)
    else:
        return None
    path = value.get("path")
    if path:
        try:
            candidate = Path(path)
            if candidate.is_absolute():
                value["path"] = candidate.resolve().relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return None
    return value


def _evidence_items(items: Any, root: Path) -> List[Dict[str, Any]]:
    values = [value for value in
              (_evidence_dict(item, root) for item in items) if value]
    return [{**value, "id": f"E{number}"}
            for number, value in enumerate(values, start=1)]


def _make_retriever(project_id: str):
    """Optional adapter for the evidence index supplied by another module."""
    project = project_registry.get(project_id)
    try:
        from .retriever import Retriever
    except ImportError as exc:
        raise HTTPException(status_code=501, detail={
            "code": "retriever_unavailable",
            "message": "项目检索组件尚未安装",
        }) from exc
    index = _get_code_index()
    _index_project_sync(project)
    return project, Retriever(index, project.root)


def _call_retriever(method, **kwargs):
    accepted = set(inspect.signature(method).parameters)
    return method(**{key: value for key, value in kwargs.items() if key in accepted})


# ---------- 工具函数 ----------

def read_text_smart(path: Path) -> Any:
    """读取文本，自动处理 UTF-8 / GBK 编码；二进制文件抛 400。"""
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise HTTPException(status_code=400, detail="这是二进制文件，无法解读")
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc), enc, data
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "unknown", data


def sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def stream_sse(sequence: StreamSequence, event_type: StreamType,
               payload: Dict[str, Any]) -> str:
    envelope = sequence.event(event_type, payload).model_dump()
    # The SSE event name and envelope.type intentionally match.  Clients use
    # job_id + monotonic seq to ignore a late event from an obsolete job.
    return sse(event_type, envelope)


def evidence_signature(items: List[Dict[str, Any]]) -> str:
    normalized = json.dumps(items, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _join_context(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part)


async def _pack_evidence(items: List[Any], base_messages: List[Dict[str, str]],
                         output_tokens: int, catalog: EvidenceCatalog,
                         evidence_budget_tokens: Optional[int] = None):
    """Pack with the active tokenizer, then assign citation IDs."""
    cfg = get_config()
    serialized = json.dumps(base_messages, ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"))
    try:
        base_tokens = await llm.count_tokens(serialized)
    except Exception:
        # Tokenization is an optimization boundary; generation can continue
        # with a conservative local estimate if the auxiliary route fails.
        base_tokens = max(1, len(serialized) // 3)
    safe_context = max(0, int(int(cfg["llama"]["ctx_size"]) * 0.9))
    if evidence_budget_tokens is not None:
        safe_context = min(
            safe_context,
            int(base_tokens) + int(output_tokens) + max(0, int(evidence_budget_tokens)),
        )
    packer = ContextPacker(
        token_counter=lambda text: max(1, len(text) // 3),
        context_window_tokens=safe_context,
        output_reserve_tokens=int(output_tokens),
        system_reserve_tokens=int(base_tokens),
        history_reserve_tokens=0,
    )
    try:
        packed = await packer.pack_async(items, llm.count_tokens)
    except Exception:
        packed = packer.pack(items)
    labelled = catalog.add([item.to_dict() for item in packed.evidence])
    return packed, labelled, EvidenceCatalog.prompt_text(labelled)


def _filtered_text(text: str, valid_ids: Any):
    citation_filter = CitationFilter(set(valid_ids))
    filtered = citation_filter.feed(text) + citation_filter.flush()
    return filtered, citation_filter.invalid_ids


async def _retrieve_evidence(project_id: str, query: str,
                             relative_path: str, limit: int = 12) -> List[Any]:
    """Best-effort evidence retrieval; missing optional component is explicit.

    Generation can still use the existing AST context while the index is being
    introduced, but import/runtime failures are never allowed to crash startup.
    """
    try:
        project, retriever = await asyncio.get_running_loop().run_in_executor(
            None, _make_retriever, project_id)
    except HTTPException as exc:
        if exc.status_code == 501:
            return []
        raise
    try:
        items = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _call_retriever(
                retriever.retrieve, query=query, current_file=relative_path, limit=limit))
    except Exception:
        diagnostic_id = _diagnostic_id()
        logger.exception("evidence retrieval failed diagnostic_id=%s project_id=%s",
                         diagnostic_id, project_id)
        return []
    # Keep the validated Evidence objects so ContextPacker can apply token-aware
    # ranking. Request-local cataloguing adds stable public ids later.
    return [item for item in items if _evidence_dict(item, project.root) is not None]


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ---------- 基础信息 ----------

@router.get("/health")
async def health() -> Dict[str, Any]:
    ready = await llm.health_check()
    st = llama_launcher.status()
    if ready and st["phase"] not in ("ready", "external"):
        st["phase"] = "ready"
    if not ready and st["phase"] not in ("starting", "external"):
        # 被动自愈：UI 轮询发现服务掉线时自动尝试拉起（内部有锁与冷却保护）
        llama_launcher.schedule_ensure_running()
        st = llama_launcher.status()
    llama_cfg = get_config()["llama"]
    return {
        "app_version": APP_VERSION,
        "model": model_id(),
        "llama": {"ready": ready, "phase": st["phase"], "detail": st["detail"]},
        "capabilities": {
            "native_chat": llama_cfg.get("protocol") == "chat_completions",
            "thinking": llm.is_thinking_model(llama_cfg),
            "structured_output": True,
            "read_only_tools": True,
        },
        "context_tokens": int(llama_cfg["ctx_size"]),
        "scheduler": {
            "profile": "8gb-single-slot" if int(llama_cfg.get("parallel", 1)) == 1
            else "high-vram-parallel",
            "parallel": int(llama_cfg.get("parallel", 1)),
        },
        "security": {
            "loopback_only": True,
            "session_required": True,
            "model_api_key": True,
        },
        "thinking": {
            "enabled": bool(llama_cfg.get("thinking", False)),
            "supported": llm.is_thinking_model(llama_cfg),
        },
    }


@router.get("/config")
async def config_info() -> Dict[str, Any]:
    cfg = get_config()
    return {
        "model": model_id(),
        "ctx_size": cfg["llama"]["ctx_size"],
        "temperature": cfg["llama"]["temperature"],
        "cache": cache.stats(),
    }


# ---------- 目录与文件 ----------

@router.post("/projects/open", response_model=ProjectOpenResponse)
async def open_project(body: ProjectOpenRequest) -> ProjectOpenResponse:
    """The sole public endpoint accepting an absolute filesystem path."""
    project = project_registry.open(body.path)
    with _INDEX_MANAGER_LOCK:
        status = _INDEX_STATUS.get(project.project_id)
    if status is None:
        _schedule_index(project)
        public_status = {
            "state": "building",
            "files_indexed": 0,
            "files_total": 0,
            "message": "正在建立增量代码索引",
        }
    else:
        public_status = _public_index_status(status)
    return ProjectOpenResponse(
        project_id=project.project_id,
        name=project.name,
        index_status=public_status,
    )


@router.get("/projects/{project_id}/files/{relative_path:path}")
async def project_file(project_id: str, relative_path: str) -> Dict[str, Any]:
    result = _project_file(project_id, relative_path)
    _schedule_index(result.project, force=True)
    return _public_file(result)


@router.get("/projects/{project_id}/browse")
@router.get("/projects/{project_id}/browse/{relative_path:path}")
async def project_browse(project_id: str, relative_path: str = "") -> Dict[str, Any]:
    resolved = project_registry.resolve_directory(project_id, relative_path)
    entries: List[Dict[str, Any]] = []
    try:
        children = sorted(resolved.path.iterdir(), key=lambda item: item.name.casefold())
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={
            "code": "project_directory_forbidden",
            "message": "无权限访问项目目录",
        }) from exc
    for child in children:
        try:
            is_dir = child.is_dir()
            if is_dir and (child.name in SKIP_DIRS or child.name.startswith(".")):
                continue
            relative = child.relative_to(resolved.project.root).as_posix()
            entries.append({
                "name": child.name,
                "relative_path": relative,
                "is_dir": is_dir,
                "is_code": (not is_dir and child.suffix.lower() in segmenter.CODE_EXTS),
            })
        except OSError:
            continue
    return {
        "project_id": project_id,
        "relative_path": resolved.relative_path,
        "entries": entries,
    }


@router.get("/projects/{project_id}/structure/{relative_path:path}")
async def project_structure(project_id: str, relative_path: str) -> Dict[str, Any]:
    result = _project_file(project_id, relative_path)
    text, _, _ = read_text_smart(result.path)
    seg_result = segmenter.segment_file(text, result.path.suffix)
    return {
        "project_id": project_id,
        "path": result.relative_path,
        "language": seg_result["language"],
        "strategy": seg_result["strategy"],
        "total_lines": seg_result["total_lines"],
        "outline": seg_result["outline"],
        "segments": [{
            "id": s["id"], "kind": s["kind"], "title": s["title"],
            "start_line": s["start_line"], "end_line": s["end_line"],
            # Cache keys include the complete prompt and validated evidence;
            # structure inspection deliberately does not run retrieval merely
            # to guess whether a later generation request will hit.
            "cached_simple": False,
            "cached_detailed": False,
        } for s in seg_result["segments"]],
        "overview_cached": False,
    }


@router.get("/diagnostics")
async def diagnostic_snapshot() -> Dict[str, Any]:
    """Source-free local performance counters for troubleshooting."""
    return {
        "metrics": diagnostics.snapshot(),
        "model": llama_launcher.status(),
    }


@router.get("/projects/{project_id}/search")
async def search_project(project_id: str, q: str = Query(..., min_length=1),
                         kind: str = Query("text", pattern="^(file|symbol|text)$"),
                         limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    project, retriever = await asyncio.get_running_loop().run_in_executor(
        None, _make_retriever, project_id)
    method = {
        "file": retriever.search_files,
        "symbol": retriever.search_symbols,
        "text": retriever.retrieve,
    }[kind]
    items = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _call_retriever(method, query=q, limit=limit))
    evidence = _evidence_items(items, project.root)
    return {"project_id": project_id, "items": evidence}


class ExploreStepBody(BaseModel):
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ExploreBody(BaseModel):
    steps: List[ExploreStepBody] = Field(min_length=1, max_length=3)


@router.post("/projects/{project_id}/explore")
async def explore_project(project_id: str, body: ExploreBody) -> Dict[str, Any]:
    """Run at most three code-enforced, read-only retrieval operations."""
    project, retriever = await asyncio.get_running_loop().run_in_executor(
        None, _make_retriever, project_id)
    explorer = ReadOnlyExplorer(project.root, retriever)
    try:
        results = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: explorer.run([
                ExplorationRequest(step.tool, step.arguments) for step in body.steps
            ]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": "invalid_exploration",
            "message": str(exc),
        }) from exc
    return {"project_id": project_id, "results": results}


@router.get("/projects/{project_id}/definitions")
async def project_definitions(project_id: str, path: str = Query(...),
                              line: int = Query(..., ge=1),
                              column: int = Query(1, ge=1),
                              symbol: Optional[str] = Query(None),
                              limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    resolved = _project_file(project_id, path)
    project, retriever = await asyncio.get_running_loop().run_in_executor(
        None, _make_retriever, project_id)
    query = symbol or _symbol_at(resolved.path, line, column)
    if not query:
        return {"project_id": project_id, "items": []}
    items = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _call_retriever(
            retriever.definitions, symbol=query,
            current_file=resolved.relative_path, limit=limit))
    evidence = _evidence_items(items, project.root)
    return {"project_id": project_id, "items": evidence}


@router.get("/projects/{project_id}/references")
async def project_references(project_id: str, path: str = Query(...),
                             line: int = Query(..., ge=1),
                             column: int = Query(1, ge=1),
                             symbol: Optional[str] = Query(None),
                             limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
    resolved = _project_file(project_id, path)
    project, retriever = await asyncio.get_running_loop().run_in_executor(
        None, _make_retriever, project_id)
    query = symbol or _symbol_at(resolved.path, line, column)
    if not query:
        return {"project_id": project_id, "items": []}
    items = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _call_retriever(
            retriever.references, symbol=query,
            current_file=resolved.relative_path, limit=limit))
    evidence = _evidence_items(items, project.root)
    return {"project_id": project_id, "items": evidence}


def _symbol_at(path: Path, line: int, column: int) -> Optional[str]:
    text, _, _ = read_text_smart(path)
    lines = text.splitlines()
    if line > len(lines):
        return None
    value = lines[line - 1]
    index = min(max(column - 1, 0), len(value))
    allowed = set(string.ascii_letters + string.digits + "_")
    start = index
    while start > 0 and value[start - 1] in allowed:
        start -= 1
    end = index
    while end < len(value) and value[end] in allowed:
        end += 1
    symbol = value[start:end]
    return symbol if symbol and not symbol[0].isdigit() else None

@router.get("/drives")
async def drives() -> Dict[str, Any]:
    result = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    return {"drives": result}


@router.get("/browse")
async def browse(path: str = Query(...)) -> Dict[str, Any]:
    """Protected absolute-path directory picker; never reads file contents."""
    p = Path(path)
    if not p.is_absolute() or not p.exists() or not p.is_dir():
        raise HTTPException(status_code=404, detail=f"目录不存在：{path}")
    dirs: List[Dict[str, Any]] = []
    files: List[Dict[str, Any]] = []
    try:
        for entry in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            name = entry.name
            try:
                if entry.is_dir():
                    if name in SKIP_DIRS or name.startswith("."):
                        continue
                    dirs.append({"name": name})
                else:
                    ext = entry.suffix.lower()
                    files.append({
                        "name": name,
                        "ext": ext,
                        "size": entry.stat().st_size,
                        "is_code": ext in segmenter.CODE_EXTS,
                    })
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"无权限访问：{path}")
    return {"path": str(p), "dirs": dirs, "files": files}


@router.get("/file", include_in_schema=False)
async def get_file() -> Dict[str, Any]:
    raise HTTPException(status_code=410, detail={
        "code": "absolute_file_api_removed",
        "message": "请改用 /api/projects/{project_id}/files/{relative_path}",
    })


@router.get("/structure", include_in_schema=False)
async def structure() -> Dict[str, Any]:
    raise HTTPException(status_code=410, detail={
        "code": "absolute_file_api_removed",
        "message": "请改用 /api/projects/{project_id}/structure/{relative_path}",
    })


# ---------- 项目索引 ----------

@router.get("/projects/{project_id}/index/status")
async def project_index_status(project_id: str) -> Dict[str, Any]:
    project_registry.get(project_id)
    with _INDEX_MANAGER_LOCK:
        status = _INDEX_STATUS.get(project_id)
        building = project_id in _INDEX_INFLIGHT
    if status is not None:
        payload = _public_index_status(status)
    else:
        payload = {
            "state": "building" if building else "idle",
            "files_indexed": 0,
            "files_total": 0,
            "message": "正在建立增量代码索引" if building else "索引尚未开始",
        }
    return {"project_id": project_id, "index_status": payload}

@router.get("/projects/{project_id}/summary")
async def project_summary(project_id: str) -> Dict[str, Any]:
    """Refresh the persistent index and return its complete safe coverage."""
    project = project_registry.get(project_id)
    status = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _index_project_sync(project, force=True))
    return {"project_id": project_id, "index": _public_index_status(status)}


# ---------- 模型管理 ----------

@router.get("/models")
async def list_models() -> Dict[str, Any]:
    current = model_id()
    mdir = resolve_path("models")
    models: List[Dict[str, Any]] = []
    if mdir.is_dir():
        for f in sorted(mdir.glob("*.gguf")):
            models.append({"name": f.name,
                           "size_gb": round(f.stat().st_size / 1e9, 2)})
    if current not in [m["name"] for m in models]:
        models.insert(0, {"name": current, "size_gb": None})
    return {"current": current, "models": models}


class SwitchBody(BaseModel):
    name: str


@router.post("/models/switch")
async def switch_model(body: SwitchBody) -> Dict[str, Any]:
    if (not body.name or "\x00" in body.name
            or Path(body.name).name != body.name
            or "/" in body.name or "\\" in body.name):
        raise HTTPException(status_code=400, detail={
            "code": "invalid_model_name",
            "message": "模型名称必须是 models 目录内的 .gguf 文件名",
        })
    mdir = resolve_path("models").resolve()
    try:
        target = (mdir / body.name).resolve(strict=True)
        target.relative_to(mdir)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail={
            "code": "model_not_found",
            "message": f"模型文件不存在：{body.name}",
        }) from exc
    if target.suffix.lower() != ".gguf" or not target.is_file():
        raise HTTPException(status_code=404, detail=f"模型文件不存在：{body.name}")
    if body.name == model_id() and await llm.health_check():
        return {"ok": True, "model": body.name, "already_active": True}

    def _upd(raw: Dict[str, Any]) -> None:
        raw.setdefault("llama", {})["model"] = body.name

    update_config_file(_upd)
    await llama_launcher.stop_async()
    llama_launcher.reset_cooldown()
    asyncio.get_running_loop().create_task(llama_launcher.ensure_running())
    return {"ok": True, "model": body.name}


class ThinkingBody(BaseModel):
    enabled: bool


@router.post("/thinking")
async def set_thinking(body: ThinkingBody) -> Dict[str, Any]:
    """开关思考模式。写回 config.json 即刻生效（提示词层实现，无需重启模型服务）。"""

    def _upd(raw: Dict[str, Any]) -> None:
        raw.setdefault("llama", {})["thinking"] = body.enabled

    update_config_file(_upd)
    llama_cfg = get_config()["llama"]
    return {
        "enabled": bool(llama_cfg.get("thinking", False)),
        "supported": llm.is_thinking_model(llama_cfg),
    }


# ---------- 最近打开 ----------

@router.get("/recents")
async def recents() -> Dict[str, Any]:
    # Absolute paths are accepted only through /browse and /projects/open and
    # are never echoed by the API after registration.
    return {"recents": []}


class PathBody(BaseModel):
    path: str


@router.post("/recents")
async def add_recent(_: PathBody) -> Dict[str, Any]:
    raise HTTPException(status_code=410, detail={
        "code": "absolute_recent_api_removed",
        "message": "最近项目由不暴露绝对路径的客户端状态替代",
    })


# ---------- 流式解读 ----------

@router.post("/explain")
async def explain(request: Request, body: ExplainRequest) -> StreamingResponse:
    resolved = _project_file(body.project_id, body.relative_path)
    p = resolved.path
    relative_path = resolved.relative_path
    project_root = str(resolved.project.root)
    text, _, _ = read_text_smart(p)
    seg_result = segmenter.segment_file(text, p.suffix)
    segments = seg_result["segments"]
    language = seg_result["language"]
    display_name = p.name
    cfg = get_config()["explain"]
    sequence = StreamSequence(scope_id=relative_path)

    # 项目符号索引（跨文件上下文），在线程池构建避免阻塞
    proj_idx = await asyncio.get_running_loop().run_in_executor(
        None, project_index.get_index, project_root)
    rel_file = relative_path

    force = body.force
    force_all = force == "all"
    force_ids = set(force) if isinstance(force, list) else set()

    def need_regen(item_id: str) -> bool:
        return force_all or item_id in force_ids

    # 解读范围与每段生效的模式：targets=None 解读全部（用全局模式）；
    # 否则只解读列出的段，target 里的 mode 覆盖全局模式
    global_mode = explainer.normalize_mode(body.mode)
    if body.targets is None:
        seg_modes = {s["id"]: global_mode for s in segments}
        run_segments = list(segments)
    else:
        requested = {t.id: explainer.normalize_mode(t.mode or global_mode)
                     for t in body.targets}
        seg_modes = {s["id"]: requested[s["id"]]
                     for s in segments if s["id"] in requested}
        by_id = {s["id"]: s for s in segments}
        # The client sends the current/visible segment first. Preserve that
        # order so remaining work is a naturally pre-emptible background tail.
        ordered_ids = list(dict.fromkeys(t.id for t in body.targets if t.id in by_id))
        run_segments = [by_id[item_id] for item_id in ordered_ids]
    cache_writes: List[tuple] = []
    catalog = EvidenceCatalog()
    generation_warnings: List[str] = []
    report_segments: Dict[str, Dict[str, str]] = {}

    def pending_cache_put(*args) -> None:
        cache_writes.append(args)

    async def gen():
        try:
            yield stream_sse(sequence, "status", {
                "state": "started",
                "metadata": {
                "path": relative_path,
                "language": language,
                "strategy": seg_result["strategy"],
                "total_lines": seg_result["total_lines"],
                "model": model_id(),
                "mode": global_mode,
                "segments": [{
                    "id": s["id"], "kind": s["kind"], "title": s["title"],
                    "start_line": s["start_line"], "end_line": s["end_line"],
                    "cached_simple": False,
                    "cached_detailed": False,
                } for s in segments],
                }})

            if not await llm.health_check():
                yield stream_sse(sequence, "status", {
                    "state": "starting_model",
                    "message": "模型服务未就绪，正在自动启动（约需几十秒）…"})
                ok = await llama_launcher.ensure_running()
                if not ok:
                    st = llama_launcher.status()
                    diagnostic_id = _diagnostic_id()
                    logger.error(
                        "model unavailable diagnostic_id=%s phase=%s detail=%s",
                        diagnostic_id, st.get("phase"), st.get("detail"))
                    yield stream_sse(sequence, "error", {
                        "code": "model_unavailable",
                        "message": "模型服务不可用",
                        "details": {"diagnostic_id": diagnostic_id}})
                    return

            # 2. 文件总览
            overview_ctx = project_index.build_project_context(
                proj_idx, text, rel_file, project_root,
                # Legacy project-map builder still consumes a character ceiling;
                # the authoritative Evidence pack below is tokenizer-counted.
                max_chars=int(cfg["project_overview_context_tokens"]) * 4,
                max_symbols=5,
                dependency_depth=int(cfg["project_dependency_depth"]),
            )
            overview_evidence = await _retrieve_evidence(
                body.project_id, text[:12000], relative_path, limit=12)
            overview_base_msgs = explainer.build_overview_messages(
                display_name, text, segments, language,
                project_context=overview_ctx)
            packed_overview, labelled_overview, overview_evidence_prompt = (
                await _pack_evidence(
                    overview_evidence,
                    overview_base_msgs,
                    int(cfg["overview_max_tokens"]),
                    catalog,
                    int(cfg["project_overview_context_tokens"]),
                )
            )
            if packed_overview.warning:
                generation_warnings.append(packed_overview.warning)
            overview_msgs = explainer.build_overview_messages(
                display_name, text, segments, language,
                project_context=_join_context(overview_ctx, overview_evidence_prompt))
            ov_key = explainer.request_cache_key(
                kind="overview", relative_path=relative_path,
                messages=overview_msgs,
                evidence_signature=evidence_signature(labelled_overview))
            overview_text = cache.get(ov_key)
            if labelled_overview:
                yield stream_sse(sequence, "evidence", {
                    "items": labelled_overview,
                    "target": "overview",
                    "used_tokens": packed_overview.used_tokens,
                    "omitted_evidence": len(packed_overview.omitted),
                    "warning": packed_overview.warning,
                })
            if overview_text and not need_regen("overview"):
                llm.record_cache_hit("overview", packed_overview.used_tokens)
                overview_text, invalid = _filtered_text(
                    overview_text, catalog.valid_ids)
                if invalid:
                    generation_warnings.append(
                        "已移除无效引用：" + "、".join(sorted(invalid)))
                yield stream_sse(sequence, "delta", {
                    "text": overview_text, "target": "overview", "cached": True})
            else:
                acc: List[str] = []
                citation_filter = CitationFilter(catalog.valid_ids)
                async for piece in llm.stream_chat(
                        overview_msgs, max_tokens=cfg["overview_max_tokens"],
                        task="overview", context_tokens=packed_overview.used_tokens):
                    filtered = citation_filter.feed(piece)
                    if filtered:
                        acc.append(filtered)
                        yield stream_sse(sequence, "delta", {
                            "text": filtered, "target": "overview"})
                tail = citation_filter.flush()
                if tail:
                    acc.append(tail)
                    yield stream_sse(sequence, "delta", {
                        "text": tail, "target": "overview"})
                if citation_filter.invalid_ids:
                    generation_warnings.append(
                        "已移除无效引用："
                        + "、".join(sorted(citation_filter.invalid_ids)))
                overview_text = "".join(acc).strip()
                if not overview_text:
                    raise RuntimeError("模型返回空总览")
                pending_cache_put(ov_key, relative_path, "overview", overview_text, model_id())

            imports_summary = explainer.imports_text(segments)

            # 3. 逐段解读（只处理 run_segments，每段按各自生效的模式）
            for s in run_segments:
                if await request.is_disconnected():
                    yield stream_sse(sequence, "cancelled", {
                        "reason": "client_disconnected"})
                    return
                mode = seg_modes[s["id"]]
                proj_ctx = project_index.build_project_context(
                    proj_idx, s["code"], rel_file, project_root,
                    max_chars=int(cfg["project_segment_context_tokens"]) * 4,
                    max_symbols=8,
                    dependency_depth=int(cfg["project_dependency_depth"]),
                )
                base_msgs = explainer.build_segment_messages(
                    display_name, overview_text or "", imports_summary, s, language,
                    project_context=proj_ctx, mode=mode)
                evidence = await _retrieve_evidence(
                    body.project_id, s["code"], relative_path, limit=12)
                packed, labelled, evidence_prompt = await _pack_evidence(
                    evidence,
                    base_msgs,
                    int(cfg["segment_max_tokens_detailed"] if mode == "detailed"
                        else cfg["segment_max_tokens"]),
                    catalog,
                    int(cfg["project_segment_context_tokens"]),
                )
                if packed.warning:
                    generation_warnings.append(packed.warning)
                msgs = explainer.build_segment_messages(
                    display_name, overview_text or "", imports_summary, s, language,
                    project_context=_join_context(proj_ctx, evidence_prompt), mode=mode)
                if labelled:
                    yield stream_sse(sequence, "evidence", {
                        "items": labelled,
                        "target": s["id"],
                        "used_tokens": packed.used_tokens,
                        "omitted_evidence": len(packed.omitted),
                        "warning": packed.warning,
                    })
                key = explainer.request_cache_key(
                    kind="segment", relative_path=relative_path,
                    messages=msgs, mode=mode,
                    evidence_signature=evidence_signature(labelled))
                cached_text = cache.get(key)
                if cached_text and not need_regen(s["id"]):
                    llm.record_cache_hit("segment", packed.used_tokens)
                    cached_text, invalid = _filtered_text(
                        cached_text, catalog.valid_ids)
                    if invalid:
                        generation_warnings.append(
                            "已移除无效引用：" + "、".join(sorted(invalid)))
                    report_segments[s["id"]] = {"mode": mode, "text": cached_text}
                    yield stream_sse(sequence, "delta", {
                        "text": cached_text, "target": s["id"],
                        "cached": True, "mode": mode})
                    continue
                max_tok = (cfg["segment_max_tokens_detailed"] if mode == "detailed"
                           else cfg["segment_max_tokens"])
                acc = []
                citation_filter = CitationFilter(catalog.valid_ids)
                async for piece in llm.stream_chat(
                        msgs, max_tokens=max_tok, task="segment",
                        context_tokens=packed.used_tokens):
                    filtered = citation_filter.feed(piece)
                    if filtered:
                        acc.append(filtered)
                        yield stream_sse(sequence, "delta", {
                            "text": filtered, "target": s["id"], "mode": mode})
                tail = citation_filter.flush()
                if tail:
                    acc.append(tail)
                    yield stream_sse(sequence, "delta", {
                        "text": tail, "target": s["id"], "mode": mode})
                if citation_filter.invalid_ids:
                    generation_warnings.append(
                        "已移除无效引用："
                        + "、".join(sorted(citation_filter.invalid_ids)))
                full = "".join(acc).strip()
                if not full:
                    raise RuntimeError("模型返回空解读")
                report_segments[s["id"]] = {"mode": mode, "text": full}
                pending_cache_put(key, relative_path, "segment", full, model_id())

            for write in cache_writes:
                cache.put(*write)
            _record_report(
                body.project_id,
                relative_path,
                segmenter.sha256_text(text),
                overview_text,
                report_segments,
            )
            yield stream_sse(sequence, "complete", {
                "result": {
                    "path": relative_path,
                    "model": model_id(),
                    "thinking": bool(get_config()["llama"].get("thinking", False)),
                },
                "warnings": list(dict.fromkeys(generation_warnings)),
            })
        except asyncio.CancelledError:
            yield stream_sse(sequence, "cancelled", {"reason": "cancelled"})
        except Exception:
            diagnostic_id = _diagnostic_id()
            logger.exception("explain failed diagnostic_id=%s", diagnostic_id)
            yield stream_sse(sequence, "error", {
                "code": "generation_failed", "message": "解读中断",
                "details": {"diagnostic_id": diagnostic_id}})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


# ---------- 追问对话 ----------

@router.post("/chat")
async def chat(request: Request, body: ChatRequest) -> StreamingResponse:
    resolved = _project_file(body.project_id, body.relative_path)
    p = resolved.path
    relative_path = resolved.relative_path
    project_root = str(resolved.project.root)
    text, _, _ = read_text_smart(p)
    language = segmenter.language_for(p.suffix)
    cfg = get_config()["explain"]
    sequence = StreamSequence(scope_id=relative_path)

    proj_idx = await asyncio.get_running_loop().run_in_executor(
        None, project_index.get_index, project_root)
    rel_file = relative_path
    overview = None

    selection_code = None
    selection_range = None
    if body.selection is not None:
        lines = text.splitlines()
        s = max(1, body.selection.start_line)
        e = min(len(lines), body.selection.end_line)
        if e >= s:
            max_sel = 400
            if e - s + 1 > max_sel:
                e = s + max_sel - 1
            selection_code = "\n".join(lines[s - 1:e])
            selection_range = f"第 {s}~{e} 行"

    # 追问使用与解读一致的分层项目上下文。没有选区时仍以整个当前文件为检索线索，
    # 并额外注入完整文件或结构骨架，避免只能回答被点中的局部代码。
    context_source = selection_code if selection_code is not None else text
    proj_ctx = project_index.build_project_context(
        proj_idx, context_source, rel_file, project_root,
        question=body.question,
        max_chars=int(cfg["project_chat_context_tokens"]) * 4,
        max_symbols=10,
        dependency_depth=int(cfg["project_dependency_depth"]),
    )
    current_file_context = ""
    if selection_code is None:
        current_budget = int(cfg["chat_current_file_tokens"]) * 4
        if len(text) <= current_budget:
            current_file_context = text
        else:
            current_file_context = explainer.build_skeleton(
                segmenter.segment_file(text, p.suffix)["segments"], current_budget)

    base_msgs = explainer.build_chat_messages(
        p.name, overview, selection_code, selection_range,
        [h.model_dump() for h in body.history], body.question, language,
        project_context=proj_ctx, current_file_context=current_file_context)
    evidence = await _retrieve_evidence(
        body.project_id, context_source + "\n" + body.question,
        relative_path, limit=12)
    catalog = EvidenceCatalog()
    packed, labelled, evidence_prompt = await _pack_evidence(
        evidence, base_msgs, int(cfg["chat_max_tokens"]), catalog,
        int(cfg["project_chat_context_tokens"]))
    msgs = explainer.build_chat_messages(
        p.name, overview, selection_code, selection_range,
        [h.model_dump() for h in body.history], body.question, language,
        project_context=_join_context(proj_ctx, evidence_prompt),
        current_file_context=current_file_context)

    async def gen():
        try:
            if labelled:
                yield stream_sse(sequence, "evidence", {
                    "items": labelled,
                    "used_tokens": packed.used_tokens,
                    "omitted_evidence": len(packed.omitted),
                    "warning": packed.warning,
                })
            if not await llm.health_check():
                yield stream_sse(sequence, "status", {
                    "state": "starting_model",
                    "message": "模型服务未就绪，正在自动启动，请稍候…"})
                ok = await llama_launcher.ensure_running()
                if not ok:
                    st = llama_launcher.status()
                    diagnostic_id = _diagnostic_id()
                    logger.error(
                        "model unavailable diagnostic_id=%s phase=%s detail=%s",
                        diagnostic_id, st.get("phase"), st.get("detail"))
                    yield stream_sse(sequence, "error", {
                        "code": "model_unavailable", "message": "模型服务不可用",
                        "details": {"diagnostic_id": diagnostic_id}})
                    return
            citation_filter = CitationFilter(catalog.valid_ids)
            async for piece in llm.stream_chat(
                    msgs, max_tokens=cfg["chat_max_tokens"], task="chat",
                    context_tokens=packed.used_tokens):
                if await request.is_disconnected():
                    yield stream_sse(sequence, "cancelled", {
                        "reason": "client_disconnected"})
                    return
                filtered = citation_filter.feed(piece)
                if filtered:
                    yield stream_sse(sequence, "delta", {
                        "text": filtered, "target": "answer"})
            tail = citation_filter.flush()
            if tail:
                yield stream_sse(sequence, "delta", {
                    "text": tail, "target": "answer"})
            warnings = []
            if packed.warning:
                warnings.append(packed.warning)
            if citation_filter.invalid_ids:
                warnings.append(
                    "已移除无效引用："
                    + "、".join(sorted(citation_filter.invalid_ids)))
            yield stream_sse(sequence, "complete", {
                "result": {"path": relative_path},
                "warnings": warnings,
            })
        except asyncio.CancelledError:
            yield stream_sse(sequence, "cancelled", {"reason": "cancelled"})
        except Exception:
            diagnostic_id = _diagnostic_id()
            logger.exception("chat failed diagnostic_id=%s", diagnostic_id)
            yield stream_sse(sequence, "error", {
                "code": "generation_failed", "message": "回答中断",
                "details": {"diagnostic_id": diagnostic_id}})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


# ---------- 导出与缓存管理 ----------

@router.get("/projects/{project_id}/export/{relative_path:path}")
async def export_markdown(project_id: str, relative_path: str) -> Response:
    resolved = _project_file(project_id, relative_path)
    p = resolved.path
    text, _, _ = read_text_smart(p)
    seg_result = segmenter.segment_file(text, p.suffix)
    source_hash = segmenter.sha256_text(text)
    report = _report_for(project_id, resolved.relative_path, source_hash)
    overview = report.get("overview") if report else None
    seg_entries: Dict[str, Optional[Dict[str, str]]] = {}
    for s in seg_result["segments"]:
        seg_entries[s["id"]] = (
            report.get("segments", {}).get(s["id"]) if report else None)
    md = explainer.build_export_markdown(
        p.name, resolved.relative_path, seg_result, overview, seg_entries)
    filename = urllib.parse.quote(f"{p.stem}-代码解读.md")
    return Response(
        content=md.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.post("/projects/{project_id}/cache/clear")
async def clear_cache(project_id: str, body: PathBody) -> Dict[str, Any]:
    resolved = _project_file(project_id, body.path)
    n = cache.delete_for_file(resolved.relative_path)
    _clear_report(project_id, resolved.relative_path)
    return {"ok": True, "deleted": n}
