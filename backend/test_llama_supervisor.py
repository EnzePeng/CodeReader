import unittest

from app import llama_launcher


class LlamaSupervisorTests(unittest.TestCase):
    def test_build_args_hardens_local_server_and_uses_one_slot(self) -> None:
        cfg = {
            "server_exe": "llama/llama-server.exe",
            "model": "Qwen3.5-9B.Q4_K_M.gguf",
            "host": "127.0.0.1",
            "port": 8711,
            "ctx_size": 8192,
            "n_gpu_layers": 99,
            "alias": "local-model",
        }
        args = llama_launcher.build_args(cfg)
        self.assertIn("--api-key", args)
        self.assertIn("--cors-origins", args)
        self.assertIn("http://127.0.0.1:8710", args)
        self.assertIn("--no-webui", args)
        self.assertIn("--no-slots", args)
        self.assertEqual(args[args.index("--parallel") + 1], "1")

    def test_generation_change_makes_old_spawn_stale(self) -> None:
        supervisor = llama_launcher.LlamaSupervisor()
        first = supervisor.new_generation()
        second = supervisor.new_generation()
        self.assertFalse(supervisor.is_current(first))
        self.assertTrue(supervisor.is_current(second))


if __name__ == "__main__":
    unittest.main()
