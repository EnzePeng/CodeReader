import json
import unittest
from unittest import mock

from app import llm


class _StreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return None


class _FakeResponse:
    status_code = 200

    async def aiter_lines(self):
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "hidden"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "正文"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        for chunk in chunks:
            yield "data: " + json.dumps(chunk, ensure_ascii=False)
        yield "data: [DONE]"

    async def aread(self):
        return b""


class _FakeClient:
    def __init__(self):
        self.url = None
        self.payload = None
        self.headers = None

    def stream(self, method, url, *, json, headers):
        self.url = url
        self.payload = json
        self.headers = headers
        return _StreamContext(_FakeResponse())


class _TokenResponse:
    status_code = 200

    def json(self):
        return {"tokens": [1, 2, 3]}


class _TokenClient:
    def __init__(self):
        self.request = None

    async def post(self, url, *, json, headers):
        self.request = (url, json, headers)
        return _TokenResponse()


class ModelBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        llm._semaphore = None

    async def test_native_chat_completions_uses_model_template(self) -> None:
        client = _FakeClient()
        cfg = {
            "base_url": "http://127.0.0.1:8711",
            "alias": "local",
            "thinking": False,
            "temperature": 0.2,
            "top_p": 0.8,
            "top_k": 20,
            "protocol": "chat_completions",
        }
        with mock.patch.object(llm, "_llama_cfg", return_value=cfg), \
             mock.patch.object(llm, "_get_client", return_value=client), \
             mock.patch.object(llm, "_authorization_headers", return_value={"Authorization": "Bearer test"}):
            output = [piece async for piece in llm.stream_chat(
                [{"role": "user", "content": "hello"}], max_tokens=32
            )]

        self.assertEqual(output, ["正文"])
        self.assertTrue(client.url.endswith("/v1/chat/completions"))
        self.assertEqual(client.payload["messages"][0]["content"], "hello")
        self.assertEqual(client.payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("prompt", client.payload)

    async def test_non_thinking_profile_is_deterministic(self) -> None:
        profile = llm.generation_profile(thinking=False)
        self.assertLessEqual(profile["temperature"], 0.3)
        self.assertFalse(profile["enable_thinking"])

    async def test_token_count_uses_model_tokenizer(self) -> None:
        client = _TokenClient()
        cfg = {"base_url": "http://127.0.0.1:8711"}
        with mock.patch.object(llm, "_llama_cfg", return_value=cfg), \
             mock.patch.object(llm, "_get_client", return_value=client), \
             mock.patch.object(llm, "_authorization_headers", return_value={}):
            count = await llm.count_tokens("hello")

        self.assertEqual(count, 3)
        self.assertTrue(client.request[0].endswith("/tokenize"))
        self.assertEqual(client.request[1], {"content": "hello", "add_special": False})


if __name__ == "__main__":
    unittest.main()
