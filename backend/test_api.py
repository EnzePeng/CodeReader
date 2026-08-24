import asyncio
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.api import _ambiguous_answer, _multi_hop_answer
from app.context_packer import ContextPacker
from app.evidence import Evidence
from app.research_agent import ResearchAgent


def agent_settings(**overrides):
    values = {
        "protocol": "auto",
        "max_research_steps": 3,
        "max_tool_calls": 8,
        "max_parallel_reads": 3,
        "planner_max_tokens": 256,
        "same_call_limit": 2,
        "no_progress_limit": 2,
        "wall_time_seconds": 2,
        "tool_result_tokens": 1200,
        "tool_step_tokens": 3200,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeExplorer:
    def __init__(self, root: Path, *, fail: bool = False) -> None:
        self.project_root = root
        self.context = SimpleNamespace(index_revision=1, current_file="target.py")
        self.fail = fail

    def invoke(self, request):
        if self.fail:
            raise ValueError("tool failure")
        if request.tool == "list_files":
            return {
                "items": ["target.py"], "evidence_refs": [], "total": 1,
                "truncated": False, "next_cursor": None, "index_revision": 1,
            }
        data = (self.project_root / "target.py").read_bytes()
        return {
            "items": [{
                "path": "target.py", "start_line": 1, "end_line": 2,
                # Normalize platform newlines exactly like the production reader.
                "content": "\n".join(data.decode().splitlines()),
                "source_hash": hashlib.sha256(data).hexdigest(),
                "language": "python", "relation": "definition", "symbol": "target",
            }],
            "evidence_refs": [{"path": "target.py", "start_line": 1, "end_line": 2}],
            "total": 1, "truncated": False, "next_cursor": None, "index_revision": 1,
        }


async def _json_protocol(_requested: str) -> str:
    return "json_schema"


async def _native_protocol(_requested: str) -> str:
    return "native"


class ResearchAgentContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "target.py").write_text(
            "def target():\n    return 1\n", encoding="utf-8")

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_tool_result_then_sufficient_then_final_evidence(self) -> None:
        decisions = iter([
            {"sufficient": False, "reason": "read", "calls": [{
                "tool": "open_code_span",
                "arguments": {"path": "target.py", "start_line": 1, "end_line": 2},
            }]},
            {"sufficient": True, "reason": "done", "calls": []},
        ])

        async def decide(_messages, _schema, _max_tokens):
            return next(decisions)

        outcome = await ResearchAgent(
            _FakeExplorer(self.root), agent_settings(),
            structured_decider=decide, protocol_probe=_json_protocol,
        ).run("target 怎么实现", [], "target.py")
        self.assertEqual((outcome.steps_used, outcome.tool_calls_used), (2, 1))
        self.assertEqual(outcome.stop_reason, "sufficient")
        self.assertEqual(len(outcome.evidence), 1)
        self.assertTrue(outcome.evidence[0].validate(self.root))

    async def test_invalid_decision_gets_one_repair(self) -> None:
        calls = 0

        async def decide(_messages, _schema, _max_tokens):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"sufficient": False, "reason": "bad", "calls": "invalid"}
            return {"sufficient": True, "reason": "fixed", "calls": []}

        outcome = await ResearchAgent(
            _FakeExplorer(self.root), agent_settings(),
            structured_decider=decide, protocol_probe=_json_protocol,
        ).run("question", [], "")
        self.assertEqual(calls, 3)
        self.assertEqual(outcome.stop_reason, "sufficient")
        self.assertEqual(outcome.tool_calls_used, 1)
        self.assertTrue(any("强制继续检索" in warning for warning in outcome.warnings))

    async def test_harness_rejects_false_sufficient_for_ambiguous_and_multihop(self) -> None:
        data = (self.root / "target.py").read_bytes()
        common = {
            "content": "\n".join(data.decode().splitlines()),
            "source_hash": hashlib.sha256(data).hexdigest(),
            "language": "python", "score": 1.0,
        }
        ambiguous = [
            Evidence(
                path="target.py", start_line=1, end_line=2,
                relation="definition", symbol="target", metadata={
                    "resolution": "ambiguous_candidate", "candidate": index,
                }, **common,
            )
            for index in range(2)
        ]
        self.assertEqual(
            ResearchAgent._coverage_status("target 绑定哪个实现", ambiguous),
            "unresolved",
        )
        partial_chain = [
            Evidence(
                path="target.py", start_line=1, end_line=2,
                relation=relation, symbol=symbol, metadata={}, **common,
            )
            for relation, symbol in [("definition", "main"), ("callee", "service")]
        ]
        self.assertEqual(
            ResearchAgent._coverage_status("main 的两跳调用链", partial_chain),
            "insufficient",
        )

    async def test_native_arguments_repair_and_history_are_protocol_complete(self) -> None:
        responses = iter([
            {"role": "assistant", "content": "bad", "tool_calls": [{
                "id": "broken", "type": "function", "function": {
                    "name": "open_code_span", "arguments": "{",
                },
            }]},
            {"role": "assistant", "content": "read", "tool_calls": [{
                "id": "read-1", "type": "function", "function": {
                    "name": "open_code_span",
                    "arguments": json.dumps({
                        "path": "target.py", "start_line": 1, "end_line": 2,
                    }),
                },
            }]},
            {"role": "assistant", "content": "enough", "tool_calls": []},
        ])
        histories = []

        async def decide(messages, _tools, _max_tokens):
            histories.append(copy.deepcopy(messages))
            return next(responses)

        outcome = await ResearchAgent(
            _FakeExplorer(self.root), agent_settings(),
            native_decider=decide, protocol_probe=_native_protocol,
        ).run("target 怎么实现", [], "target.py")

        self.assertEqual(outcome.stop_reason, "sufficient")
        self.assertEqual(len(outcome.tool_events), 1)
        self.assertIn("invalid", histories[1][-1]["content"])
        assistant = next(item for item in histories[2] if item.get("tool_calls"))
        self.assertEqual([call["id"] for call in assistant["tool_calls"]], ["read-1"])
        self.assertTrue(any(item.get("tool_call_id") == "read-1" for item in histories[2]))

    async def test_repeated_no_progress_tools_stop_without_crashing(self) -> None:
        async def decide(_messages, _schema, _max_tokens):
            return {"sufficient": False, "reason": "list", "calls": [{
                "tool": "list_files", "arguments": {"pattern": "*.py"},
            }]}

        outcome = await ResearchAgent(
            _FakeExplorer(self.root), agent_settings(),
            structured_decider=decide, protocol_probe=_json_protocol,
        ).run("question", [], "")
        self.assertEqual(outcome.stop_reason, "no_progress")
        self.assertEqual(outcome.tool_calls_used, 2)

    async def test_invalid_json_degrades_once_and_uses_deterministic_tools(self) -> None:
        data = (self.root / "target.py").read_bytes()
        initial = Evidence(
            path="target.py", start_line=1, end_line=2,
            content="\n".join(data.decode().splitlines()),
            source_hash=hashlib.sha256(data).hexdigest(), language="python",
            relation="definition", symbol="target", score=1.0,
            metadata={"resolution": "exact", "symbol_id": 1},
        )
        planner_calls = 0

        async def invalid(_messages, _schema, _max_tokens):
            nonlocal planner_calls
            planner_calls += 1
            raise json.JSONDecodeError("bad planner", "", 0)

        outcome = await ResearchAgent(
            _FakeExplorer(self.root), agent_settings(),
            structured_decider=invalid, protocol_probe=_json_protocol,
        ).run("target 的两跳调用链", [initial], "target.py")

        self.assertEqual(planner_calls, 2)  # one call plus its single repair
        self.assertEqual(outcome.protocol, "deterministic")
        self.assertGreater(outcome.tool_calls_used, 0)
        self.assertLessEqual(outcome.steps_used, 3)
        self.assertTrue(any("确定性" in warning for warning in outcome.warnings))

    async def test_timeout_and_cancellation_are_bounded(self) -> None:
        async def slow(_messages, _schema, _max_tokens):
            await asyncio.sleep(1)
            return {"sufficient": True, "reason": "late", "calls": []}

        timed = await ResearchAgent(
            _FakeExplorer(self.root), agent_settings(wall_time_seconds=0.02),
            structured_decider=slow, protocol_probe=_json_protocol,
        ).run("question", [], "")
        self.assertEqual(timed.stop_reason, "timeout")

        task = asyncio.create_task(ResearchAgent(
            _FakeExplorer(self.root), agent_settings(wall_time_seconds=10),
            structured_decider=slow, protocol_probe=_json_protocol,
        ).run("question", [], ""))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class ContextWindowGateTests(unittest.TestCase):
    def test_multi_hop_final_answer_uses_exact_evidence_locators(self) -> None:
        answer = _multi_hop_answer([
            {"id": "E1", "path": "entry.py", "start_line": 3, "end_line": 4,
             "symbol": "main", "language": "python", "content": "def main(): pass",
             "metadata": {"qualified_name": "main", "kind": "function"}},
            {"id": "E2", "path": "service.py", "start_line": 3, "end_line": 5,
             "symbol": "fetch_user", "language": "python",
             "content": "def fetch_user(): pass",
             "metadata": {"qualified_name": "fetch_user", "kind": "function"}},
            {"id": "E3", "path": "repo.py", "start_line": 2, "end_line": 3,
             "symbol": "load", "language": "python", "content": "return f'DB:{key}'",
             "metadata": {"qualified_name": "Repository.load", "kind": "method"}},
        ])
        self.assertIn("`main` → `fetch_user` → `Repository.load`", answer)
        self.assertIn("service.py:3-5", answer)
        self.assertIn("DB:", answer)

    def test_ambiguous_final_answer_never_selects_one_candidate(self) -> None:
        answer = _ambiguous_answer([
            {"id": "E1", "path": "a.py", "start_line": 1, "end_line": 2,
             "symbol": "transform"},
            {"id": "E2", "path": "z.py", "start_line": 3, "end_line": 5,
             "symbol": "transform"},
        ])
        self.assertIn("无法唯一确定", answer)
        self.assertIn("a.py:1-2 [E1]", answer)
        self.assertIn("z.py:3-5 [E2]", answer)
        self.assertNotIn("绑定的是", answer)

    def test_4096_8192_16384_and_thinking_reserves_never_overflow(self) -> None:
        content = "\n".join(f"line {index}" for index in range(1, 1001))
        evidence = Evidence(
            path="large.py", start_line=1, end_line=1000, content=content,
            source_hash="hash", language="python", relation="definition",
            symbol="large", score=1.0,
        )
        for window in (4096, 8192, 16384):
            for thinking in (False, True):
                output = 1200 + (1200 if thinking else 0)
                packer = ContextPacker(
                    token_counter=lambda text: len(text.split()),
                    context_window_tokens=int(window * 0.9),
                    output_reserve_tokens=output,
                    system_reserve_tokens=500,
                    history_reserve_tokens=256,
                )
                packed = packer.pack([evidence])
                self.assertLessEqual(packed.used_tokens, int(window * 0.9))
                self.assertLessEqual(
                    packed.evidence_tokens + packed.reserved_tokens, int(window * 0.9))
                self.assertTrue(packed.evidence)


if __name__ == "__main__":
    unittest.main()
