"""HTTP API：目录浏览、文件读取、结构分析、流式解读、追问、导出。"""
import asyncio
import json
import os
import string
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from . import cache, explainer, llama_launcher, llm, project_index, segmenter
from .config import (APP_VERSION, data_dir, get_config, model_id,
                     resolve_path, update_config_file)

router = APIRouter()

SKIP_DIRS = segmenter.SKIP_DIRS


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


def validate_file(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail="必须提供绝对路径")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在：{p}")
    max_bytes = get_config()["explain"]["max_file_bytes"]
    if p.stat().st_size > max_bytes:
        raise HTTPException(status_code=400,
                            detail=f"文件超过 {max_bytes // 1000000} MB，暂不支持")
    return p


def sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _state_file() -> Path:
    return data_dir() / "state.json"


def _load_state() -> Dict[str, Any]:
    f = _state_file()
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"recents": []}


def _save_state(state: Dict[str, Any]) -> None:
    _state_file().write_text(json.dumps(state, ensure_ascii=False, indent=2),
                             encoding="utf-8")


# ---------- 基础信息 ----------

@router.get("/health")
async def health() -> Dict[str, Any]:
    ready = await llm.health_check()
    st = llama_launcher.status()
    if ready and st["phase"] not in ("ready", "external"):
        st["phase"] = "ready"
    if not ready and st["phase"] not in ("starting", "external"):
        # 被动自愈：UI 轮询发现服务掉线时自动尝试拉起（内部有锁与冷却保护）
        asyncio.get_running_loop().create_task(llama_launcher.ensure_running())
        st = llama_launcher.status()
    llama_cfg = get_config()["llama"]
    return {
        "app_version": APP_VERSION,
        "model": model_id(),
        "llama": {"ready": ready, "phase": st["phase"], "detail": st["detail"]},
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

@router.get("/drives")
async def drives() -> Dict[str, Any]:
    result = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    return {"drives": result}


@router.get("/browse")
async def browse(path: str = Query(...)) -> Dict[str, Any]:
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


@router.get("/file")
async def get_file(path: str = Query(...)) -> Dict[str, Any]:
    p = validate_file(path)
    text, encoding, _ = read_text_smart(p)
    return {
        "path": str(p),
        "name": p.name,
        "language": segmenter.language_for(p.suffix),
        "encoding": encoding,
        "line_count": len(text.splitlines()),
        "content": text,
    }


@router.get("/structure")
async def structure(path: str = Query(...),
                    project_root: Optional[str] = Query(None)) -> Dict[str, Any]:
    """文件的分段与大纲（不含解读，秒回）。"""
    p = validate_file(path)
    text, _, raw = read_text_smart(p)
    seg_result = segmenter.segment_file(text, p.suffix)
    file_hash = cache.make_key("file", segmenter.sha256_text(text))
    proj_idx = await asyncio.get_running_loop().run_in_executor(
        None, project_index.get_index, project_root)
    project_sig = project_index.context_signature(proj_idx)
    seg_meta = []
    # 两种解读模式的缓存分开报告，避免"有缓存"的含义混淆
    simple_keys = {s["id"]: explainer.segment_key(s, "simple", project_sig)
                   for s in seg_result["segments"]}
    detailed_keys = {s["id"]: explainer.segment_key(s, "detailed", project_sig)
                     for s in seg_result["segments"]}
    cached_map = cache.get_many(list(simple_keys.values()) + list(detailed_keys.values()))
    for s in seg_result["segments"]:
        seg_meta.append({
            "id": s["id"], "kind": s["kind"], "title": s["title"],
            "start_line": s["start_line"], "end_line": s["end_line"],
            "cached_simple": simple_keys[s["id"]] in cached_map,
            "cached_detailed": detailed_keys[s["id"]] in cached_map,
        })
    return {
        "path": str(p),
        "language": seg_result["language"],
        "strategy": seg_result["strategy"],
        "total_lines": seg_result["total_lines"],
        "outline": seg_result["outline"],
        "segments": seg_meta,
        "overview_cached": cache.get(
            explainer.overview_key(file_hash, project_sig)) is not None,
    }


# ---------- 项目索引 ----------

@router.get("/project/summary")
async def project_summary(root: str = Query(...)) -> Dict[str, Any]:
    """构建（或复用）项目符号索引并返回概况。打开项目时调用可预热索引。"""
    idx = await asyncio.get_running_loop().run_in_executor(
        None, project_index.get_index, root)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"目录不存在：{root}")
    return {"files": idx["files"], "symbols": len(idx["symbols"]),
            "build_ms": idx["build_ms"]}


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
    mdir = resolve_path("models")
    target = mdir / body.name
    if target.suffix.lower() != ".gguf" or not target.exists():
        raise HTTPException(status_code=404, detail=f"模型文件不存在：{body.name}")
    if body.name == model_id() and await llm.health_check():
        return {"ok": True, "model": body.name, "already_active": True}

    def _upd(raw: Dict[str, Any]) -> None:
        raw.setdefault("llama", {})["model"] = f"models/{body.name}"

    update_config_file(_upd)
    llama_launcher.stop()
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
    return {"recents": _load_state().get("recents", [])}


class PathBody(BaseModel):
    path: str


@router.post("/recents")
async def add_recent(body: PathBody) -> Dict[str, Any]:
    p = Path(body.path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=404, detail="目录不存在")
    state = _load_state()
    items = [r for r in state.get("recents", []) if r.get("path") != str(p)]
    items.insert(0, {"path": str(p), "time": time.time()})
    state["recents"] = items[:10]
    _save_state(state)
    return {"ok": True}


# ---------- 流式解读 ----------

class TargetItem(BaseModel):
    """指定要解读的单个段；mode 为空时用请求级的全局模式。"""
    id: str
    mode: Optional[str] = None  # "simple" | "detailed"


class ExplainBody(BaseModel):
    path: str
    force: Union[str, List[str]] = "none"  # "none" | "all" | ["overview", "s1", ...]
    project_root: Optional[str] = None
    mode: str = "simple"  # 全局解读模式："simple" | "detailed"
    # None = 解读全部分段；给列表 = 只解读列出的段（空列表则只生成总览）。
    # force 与 targets 正交：force 决定是否忽略缓存，targets 决定解读范围。
    targets: Optional[List[TargetItem]] = None


@router.post("/explain")
async def explain(request: Request, body: ExplainBody) -> StreamingResponse:
    p = validate_file(body.path)
    text, _, _ = read_text_smart(p)
    seg_result = segmenter.segment_file(text, p.suffix)
    segments = seg_result["segments"]
    language = seg_result["language"]
    file_hash = cache.make_key("file", segmenter.sha256_text(text))
    display_name = p.name
    cfg = get_config()["explain"]

    # 项目符号索引（跨文件上下文），在线程池构建避免阻塞
    proj_idx = await asyncio.get_running_loop().run_in_executor(
        None, project_index.get_index, body.project_root)
    rel_file = project_index.relative_of(body.project_root, str(p)) if proj_idx else None
    project_sig = project_index.context_signature(proj_idx)

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
    else:
        requested = {t.id: explainer.normalize_mode(t.mode or global_mode)
                     for t in body.targets}
        seg_modes = {s["id"]: requested[s["id"]]
                     for s in segments if s["id"] in requested}
    run_segments = [s for s in segments if s["id"] in seg_modes]

    async def gen():
        try:
            # 1. 元信息
            ov_key = explainer.overview_key(file_hash, project_sig)
            simple_keys = {s["id"]: explainer.segment_key(s, "simple", project_sig)
                           for s in segments}
            detailed_keys = {s["id"]: explainer.segment_key(s, "detailed", project_sig)
                             for s in segments}
            cached_map = cache.get_many(list(simple_keys.values()) + list(detailed_keys.values()))
            # 本次运行每段实际使用的缓存键（按各自生效模式）
            seg_keys = {sid: (detailed_keys[sid] if m == "detailed" else simple_keys[sid])
                        for sid, m in seg_modes.items()}
            yield sse("meta", {
                "path": str(p),
                "language": language,
                "strategy": seg_result["strategy"],
                "total_lines": seg_result["total_lines"],
                "model": model_id(),
                "mode": global_mode,
                "segments": [{
                    "id": s["id"], "kind": s["kind"], "title": s["title"],
                    "start_line": s["start_line"], "end_line": s["end_line"],
                    "cached_simple": simple_keys[s["id"]] in cached_map,
                    "cached_detailed": detailed_keys[s["id"]] in cached_map,
                } for s in segments],
            })

            if not await llm.health_check():
                yield sse("status", {"message": "模型服务未就绪，正在自动启动（约需几十秒）…"})
                ok = await llama_launcher.ensure_running()
                if not ok:
                    st = llama_launcher.status()
                    yield sse("error", {"message": "模型服务不可用："
                              + (st.get("detail") or "请查看 data/llama-server.log")})
                    return

            # 2. 文件总览
            overview_text = cache.get(ov_key)
            if overview_text is not None and not need_regen("overview"):
                yield sse("overview_done", {"text": overview_text, "cached": True})
            else:
                yield sse("overview_start", {})
                overview_ctx = project_index.build_project_context(
                    proj_idx, text, rel_file, body.project_root,
                    max_chars=int(cfg["project_overview_context_chars"]),
                    max_symbols=5,
                    dependency_depth=int(cfg["project_dependency_depth"]),
                )
                msgs = explainer.build_overview_messages(
                    display_name, text, segments, language,
                    project_context=overview_ctx)
                acc: List[str] = []
                async for piece in llm.stream_chat(msgs, max_tokens=cfg["overview_max_tokens"]):
                    acc.append(piece)
                    yield sse("overview_delta", {"text": piece})
                overview_text = "".join(acc).strip()
                cache.put(ov_key, str(p), "overview", overview_text, model_id())
                yield sse("overview_done", {"text": overview_text, "cached": False})

            imports_summary = explainer.imports_text(segments)

            # 3. 逐段解读（只处理 run_segments，每段按各自生效的模式）
            for s in run_segments:
                if await request.is_disconnected():
                    return
                mode = seg_modes[s["id"]]
                key = seg_keys[s["id"]]
                cached_text = cached_map.get(key)
                if cached_text is None:
                    cached_text = cache.get(key)
                if cached_text is not None and not need_regen(s["id"]):
                    yield sse("segment_done", {"id": s["id"], "text": cached_text,
                                               "cached": True, "mode": mode})
                    continue
                yield sse("segment_start", {"id": s["id"], "mode": mode})
                proj_ctx = project_index.build_project_context(
                    proj_idx, s["code"], rel_file, body.project_root,
                    max_chars=int(cfg["project_segment_context_chars"]),
                    max_symbols=8,
                    dependency_depth=int(cfg["project_dependency_depth"]),
                )
                msgs = explainer.build_segment_messages(
                    display_name, overview_text or "", imports_summary, s, language,
                    project_context=proj_ctx, mode=mode)
                max_tok = (cfg["segment_max_tokens_detailed"] if mode == "detailed"
                           else cfg["segment_max_tokens"])
                acc = []
                async for piece in llm.stream_chat(msgs, max_tokens=max_tok):
                    acc.append(piece)
                    yield sse("segment_delta", {"id": s["id"], "text": piece, "mode": mode})
                full = "".join(acc).strip()
                cache.put(key, str(p), "segment", full, model_id())
                yield sse("segment_done", {"id": s["id"], "text": full,
                                           "cached": False, "mode": mode})

            yield sse("done", {})
        except Exception as e:
            yield sse("error", {"message": f"解读中断：{e}"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


# ---------- 追问对话 ----------

class Selection(BaseModel):
    start_line: int
    end_line: int


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatBody(BaseModel):
    path: str
    question: str
    selection: Optional[Selection] = None
    history: List[ChatMessage] = []
    project_root: Optional[str] = None


@router.post("/chat")
async def chat(request: Request, body: ChatBody) -> StreamingResponse:
    p = validate_file(body.path)
    text, _, _ = read_text_smart(p)
    language = segmenter.language_for(p.suffix)
    file_hash = cache.make_key("file", segmenter.sha256_text(text))
    cfg = get_config()["explain"]

    proj_idx = await asyncio.get_running_loop().run_in_executor(
        None, project_index.get_index, body.project_root)
    rel_file = project_index.relative_of(body.project_root, str(p)) if proj_idx else None
    project_sig = project_index.context_signature(proj_idx)
    overview = cache.get(explainer.overview_key(file_hash, project_sig))

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
        proj_idx, context_source, rel_file, body.project_root,
        question=body.question,
        max_chars=int(cfg["project_chat_context_chars"]),
        max_symbols=10,
        dependency_depth=int(cfg["project_dependency_depth"]),
    )
    current_file_context = ""
    if selection_code is None:
        current_budget = int(cfg["chat_current_file_chars"])
        if len(text) <= current_budget:
            current_file_context = text
        else:
            current_file_context = explainer.build_skeleton(
                segmenter.segment_file(text, p.suffix)["segments"], current_budget)

    msgs = explainer.build_chat_messages(
        p.name, overview, selection_code, selection_range,
        [h.model_dump() for h in body.history], body.question, language,
        project_context=proj_ctx, current_file_context=current_file_context)

    async def gen():
        try:
            if not await llm.health_check():
                yield sse("status", {"message": "模型服务未就绪，正在自动启动，请稍候…"})
                ok = await llama_launcher.ensure_running()
                if not ok:
                    st = llama_launcher.status()
                    yield sse("error", {"message": "模型服务不可用："
                              + (st.get("detail") or "请查看 data/llama-server.log")})
                    return
            async for piece in llm.stream_chat(msgs, max_tokens=cfg["chat_max_tokens"]):
                if await request.is_disconnected():
                    return
                yield sse("delta", {"text": piece})
            yield sse("done", {})
        except Exception as e:
            yield sse("error", {"message": f"回答中断：{e}"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


# ---------- 导出与缓存管理 ----------

@router.get("/export")
async def export_markdown(path: str = Query(...),
                          project_root: Optional[str] = Query(None)) -> Response:
    p = validate_file(path)
    text, _, _ = read_text_smart(p)
    seg_result = segmenter.segment_file(text, p.suffix)
    file_hash = cache.make_key("file", segmenter.sha256_text(text))
    proj_idx = await asyncio.get_running_loop().run_in_executor(
        None, project_index.get_index, project_root)
    project_sig = project_index.context_signature(proj_idx)
    overview = cache.get(explainer.overview_key(file_hash, project_sig))
    # 每段可能存在简单/逐行两种缓存，导出取最近生成的那份并标注模式
    seg_entries: Dict[str, Optional[Dict[str, str]]] = {}
    for s in seg_result["segments"]:
        mode_keys = {m: explainer.segment_key(s, m, project_sig)
                     for m in ("simple", "detailed")}
        hit = cache.get_newest(list(mode_keys.values()))
        if hit is None:
            seg_entries[s["id"]] = None
        else:
            mode = "detailed" if hit[0] == mode_keys["detailed"] else "simple"
            seg_entries[s["id"]] = {"mode": mode, "text": hit[1]}
    md = explainer.build_export_markdown(p.name, str(p), seg_result, overview, seg_entries)
    filename = urllib.parse.quote(f"{p.stem}-代码解读.md")
    return Response(
        content=md.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.post("/cache/clear")
async def clear_cache(body: PathBody) -> Dict[str, Any]:
    n = cache.delete_for_file(body.path)
    return {"ok": True, "deleted": n}
