"""验证经过 llm.stream_chat（ChatML 预填闭合思考块）后，模型直接输出正文。"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app import llm  # noqa: E402


async def main() -> None:
    msgs = [
        {"role": "system", "content": "你是一个中文技术助手，回答简洁。"},
        {"role": "user", "content": "用一句话解释什么是快速排序"},
    ]
    t0 = time.time()
    text = await llm.complete(msgs, max_tokens=200)
    dt = time.time() - t0
    print(f"用时 {dt:.1f}s, 长度 {len(text)}")
    print("输出:", text[:200])
    assert text.strip(), "输出为空！思考模式仍未关闭"
    assert "<think>" not in text, "输出中泄漏了思考标签"
    print("OK: 正文直出，无思考损耗")


if __name__ == "__main__":
    asyncio.run(main())
