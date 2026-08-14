import unittest
import asyncio


class ContextPackerTest(unittest.TestCase):
    @staticmethod
    def _evidence(path: str, content: str, relation: str, score: float):
        from app.evidence import Evidence

        return Evidence(
            path=path,
            start_line=1,
            end_line=1,
            content=content,
            source_hash="a" * 64,
            language="python",
            relation=relation,
            symbol=path,
            score=score,
        )

    def test_priority_packing_respects_total_token_budget(self) -> None:
        from app.context_packer import ContextPacker

        counter = lambda text: len(text.split())
        high = self._evidence("high.py", "high evidence is concise", "definition", 1.0)
        low = self._evidence(
            "low.py", "low evidence has many words and should not displace exact definition",
            "text", 0.2,
        )
        packer = ContextPacker(
            token_counter=counter,
            context_window_tokens=30,
            output_reserve_tokens=6,
            system_reserve_tokens=3,
            history_reserve_tokens=2,
        )
        packed = packer.pack([low, high])

        self.assertLessEqual(packed.used_tokens, 30)
        self.assertEqual("high.py", packed.evidence[0].path)
        self.assertGreaterEqual(packed.reserved_tokens, 11)
        self.assertEqual(counter(packed.text), packed.evidence_tokens)

    def test_oversized_item_is_omitted_but_smaller_item_can_fit(self) -> None:
        from app.context_packer import ContextPacker

        counter = lambda text: len(text)
        huge = self._evidence("huge.py", "x" * 500, "definition", 1.0)
        small = self._evidence("small.py", "ok", "text", 0.2)
        packed = ContextPacker(counter, 160, 20, 10, 10).pack([small, huge])

        self.assertEqual(["small.py"], [item.path for item in packed.evidence])
        self.assertEqual(["huge.py"], [item.path for item in packed.omitted])
        self.assertIn("省略", packed.warning)

    def test_async_packer_uses_model_token_counter(self) -> None:
        from app.context_packer import ContextPacker

        calls = []

        async def model_counter(text: str) -> int:
            calls.append(text)
            return len(text.split())

        evidence = [self._evidence("a.py", "one two", "definition", 1.0)]
        packer = ContextPacker(
            token_counter=lambda _: 999,
            context_window_tokens=20,
            output_reserve_tokens=2,
            system_reserve_tokens=2,
            history_reserve_tokens=2,
        )
        packed = asyncio.run(packer.pack_async(evidence, model_counter))

        self.assertEqual(len(packed.evidence), 1)
        self.assertTrue(calls)
        self.assertLessEqual(packed.used_tokens, 20)

    def test_reservations_can_exhaust_context_without_overflow(self) -> None:
        from app.context_packer import ContextPacker

        item = self._evidence("a.py", "content", "definition", 1.0)
        packed = ContextPacker(lambda text: len(text), 20, 10, 8, 7).pack([item])
        self.assertEqual([], packed.evidence)
        self.assertEqual([item], packed.omitted)
        self.assertLessEqual(packed.used_tokens, 20)
        self.assertIn("预算", packed.warning)


if __name__ == "__main__":
    unittest.main()
