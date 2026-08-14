import unittest

from app.diagnostics import Diagnostics, GenerationMetric


class DiagnosticsTests(unittest.TestCase):
    def test_snapshot_aggregates_performance_without_source_text(self) -> None:
        diagnostics = Diagnostics(capacity=3)
        diagnostics.record_generation(GenerationMetric(
            task="chat", queue_ms=10, ttft_ms=200, duration_ms=800,
            output_tokens=80, context_tokens=1200, cache_hit=False,
        ))
        diagnostics.record_generation(GenerationMetric(
            task="chat", queue_ms=0, ttft_ms=100, duration_ms=400,
            output_tokens=40, context_tokens=600, cache_hit=True,
        ))

        snapshot = diagnostics.snapshot()

        self.assertEqual(snapshot["generation"]["count"], 2)
        self.assertEqual(snapshot["generation"]["cache_hit_rate"], 0.5)
        self.assertEqual(snapshot["generation"]["median_ttft_ms"], 150.0)
        self.assertNotIn("content", str(snapshot).lower())

    def test_capacity_discards_old_samples(self) -> None:
        diagnostics = Diagnostics(capacity=2)
        for value in (1, 2, 3):
            diagnostics.record_index(duration_ms=value, files=1, failed=0)
        self.assertEqual(diagnostics.snapshot()["index"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
