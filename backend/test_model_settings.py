import unittest
from unittest import mock

from app import api, model_settings


def tuning(**overrides):
    values = {
        "ctx_size": 8192,
        "n_gpu_layers": 99,
        "cache_type_k": "q8_0",
        "cache_type_v": "q8_0",
        "parallel": 1,
        "temperature": 0.2,
        "top_p": 0.9,
        "top_k": 20,
        "thinking": False,
    }
    values.update(overrides)
    return model_settings.ModelTuningValues.model_validate(values)


def recommendation(values=None):
    selected = values or tuning()
    reasons = {key: "适合当前硬件" for key in selected.model_dump()}
    return {
        "values": selected.model_dump(),
        "summary": "优先保证单用户代码阅读稳定运行。",
        "rationale": reasons,
        "warnings": ["增大上下文会增加 KV 缓存占用。"],
        "confidence": "medium",
    }


class ModelSettingsTests(unittest.TestCase):
    def test_context_window_requires_1024_multiple(self) -> None:
        with self.assertRaisesRegex(ValueError, "multiple of 1024"):
            tuning(ctx_size=9000)

    def test_public_recommendation_is_model_attributed_and_validated(self) -> None:
        result = model_settings.public_recommendation(recommendation())
        self.assertEqual(result["source"], "model")
        self.assertEqual(result["values"]["ctx_size"], 8192)
        self.assertIn("generated_at", result)

    def test_flat_live_recommendation_gets_explanatory_copy(self) -> None:
        result = model_settings.public_recommendation(
            tuning(ctx_size=16384).model_dump(),
            {"model": "test.gguf", "hardware": {"gpus": [{"name": "Test GPU"}]}},
        )
        self.assertEqual(result["values"]["ctx_size"], 16384)
        self.assertIn("Test GPU", result["summary"])
        self.assertIn("ctx_size", result["rationale"])


class ModelSettingsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_recommendation_does_not_apply_configuration(self) -> None:
        with mock.patch.object(api.llm, "health_check", new=mock.AsyncMock(return_value=True)), \
             mock.patch.object(api.llm, "structured_complete", new=mock.AsyncMock(
                 return_value=recommendation())), \
             mock.patch.object(api.model_settings, "settings_payload", return_value={
                 "model": "test.gguf", "current": tuning().model_dump(), "hardware": {},
             }), \
             mock.patch.object(api, "update_config_file") as update:
            result = await api.recommend_model_settings()

        self.assertEqual(result["source"], "model")
        update.assert_not_called()

    async def test_resource_change_persists_and_restarts_once(self) -> None:
        requested = tuning(ctx_size=16384)
        captured = {}

        def update(updater):
            raw = {}
            updater(raw)
            captured.update(raw)

        with mock.patch.object(api.model_settings, "current_values", return_value=tuning()), \
             mock.patch.object(api, "update_config_file", side_effect=update), \
             mock.patch.object(api.llama_launcher, "stop_async", new=mock.AsyncMock()) as stop, \
             mock.patch.object(api.llm, "close_client", new=mock.AsyncMock()) as close, \
             mock.patch.object(api.llama_launcher, "reset_cooldown") as reset, \
             mock.patch.object(api.llama_launcher, "schedule_ensure_running") as start, \
             mock.patch.object(api.model_settings, "settings_payload", return_value={
                 "model": "test.gguf", "current": requested.model_dump(), "hardware": {},
             }):
            result = await api.set_model_settings(requested)

        self.assertTrue(result["restarting"])
        self.assertEqual(captured["llama"]["ctx_size"], 16384)
        stop.assert_awaited_once()
        close.assert_awaited_once()
        reset.assert_called_once()
        start.assert_called_once()

    async def test_sampling_only_change_does_not_restart(self) -> None:
        requested = tuning(temperature=0.3)
        with mock.patch.object(api.model_settings, "current_values", return_value=tuning()), \
             mock.patch.object(api, "update_config_file"), \
             mock.patch.object(api.llama_launcher, "stop_async", new=mock.AsyncMock()) as stop, \
             mock.patch.object(api.model_settings, "settings_payload", return_value={
                 "model": "test.gguf", "current": requested.model_dump(), "hardware": {},
             }):
            result = await api.set_model_settings(requested)

        self.assertFalse(result["restarting"])
        stop.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
