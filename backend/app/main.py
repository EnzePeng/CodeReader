"""FastAPI 应用：API 路由 + 前端静态文件托管 + llama-server 生命周期。"""
import asyncio
import atexit
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import api, llama_launcher
from .config import APP_VERSION, static_dir


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(llama_launcher.ensure_started())
    yield
    task.cancel()
    llama_launcher.stop()


app = FastAPI(title="CodeReader", version=APP_VERSION, lifespan=lifespan)
app.include_router(api.router, prefix="/api")

_static = static_dir()
if _static.exists():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")

atexit.register(llama_launcher.stop)
