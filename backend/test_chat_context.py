import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from app.code_index import CodeIndex
from app.context_broker import ContextBroker
from app.conversation import ConversationStore, EvidenceAnchor
from app.evidence import Evidence
from app.retriever import Retriever


class ContextBrokerAndConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        (self.root / "a.py").write_text(
            "def foo(value):\n    return 'wrong'\n", encoding="utf-8")
        long_body = ["def foo(value):", "    total = value"]
        long_body.extend("    total += 1" for _ in range(260))
        long_body.append("    return total")
        (self.root / "z.py").write_text("\n".join(long_body) + "\n", encoding="utf-8")
        (self.root / "consumer.py").write_text(
            "from z import foo\n\ndef run(value):\n    return foo(value)\n", encoding="utf-8")
        (self.root / "dynamic.py").write_text(
            "import a\nimport z\n\ndef choose(module, value):\n"
            "    return module.foo(value)\n",
            encoding="utf-8",
        )
        self.index = CodeIndex(base / "index.db")
        self.index.index_project(self.root)
        self.retriever = Retriever(self.index, self.root)
        self.broker = ContextBroker(self.index, self.retriever, self.root, repo_map_tokens=900)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_import_disambiguation_and_long_function_windows_are_prefetched(self) -> None:
        result = self.broker.collect(
            "foo 这个函数内部怎么实现", "consumer.py", selection=(4, 4))
        exact = [item for item in result.evidence
                 if item.metadata.get("resolution") == "exact" and item.symbol == "foo"]
        self.assertTrue(result.sufficient)
        self.assertTrue(result.direct_target)
        self.assertTrue(exact)
        self.assertEqual({item.path for item in exact}, {"z.py"})
        windows = [item for item in result.evidence
                   if item.path == "z.py" and item.metadata.get("continuation")]
        self.assertGreaterEqual(len(windows), 2)
        self.assertTrue(windows[0].content.startswith("def foo"))
        self.assertIn("z.py", result.repository_map)
        self.assertIn("foo", result.repository_map)
        self.assertFalse(any(
            item.path == "a.py" and item.relation == "definition"
            for item in result.evidence
        ))

    def test_stale_anchor_is_reindexed_rejected_and_retrieval_uses_new_source(self) -> None:
        path = self.root / "z.py"
        before = path.read_bytes()
        stat = path.stat()
        anchor = EvidenceAnchor(
            "z.py", 1, 10, hashlib.sha256(before).hexdigest(), "foo")
        changed = before.replace(b"total += 1", b"total += 2")
        self.assertEqual(len(before), len(changed))
        path.write_bytes(changed)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        result = self.broker.collect(
            "foo 这个函数内部怎么实现", "consumer.py", anchors=[anchor])

        self.assertTrue(any("已拒绝" in warning for warning in result.warnings))
        self.assertTrue(all(item.validate(self.root) for item in result.evidence))
        self.assertTrue(any("total += 2" in item.content for item in result.evidence
                            if item.path == "z.py"))

    def test_explicit_multi_hop_question_requires_research(self) -> None:
        result = self.broker.collect("foo 的两跳调用链和最终调用是什么", "consumer.py")
        self.assertFalse(result.sufficient)
        self.assertFalse(result.direct_target)
        self.assertIn("多跳", result.reason)

    def test_direct_callee_question_does_not_become_caller_focus(self) -> None:
        direct = self.broker.collect("它调用的 foo 具体怎么实现", "consumer.py")
        callers = self.broker.collect("谁调用了 foo", "consumer.py")
        self.assertTrue(direct.direct_target)
        self.assertFalse(callers.direct_target)

    def test_one_exact_identifier_does_not_hide_another_ambiguous_identifier(self) -> None:
        result = self.broker.collect(
            "choose 里的 module.foo 实际绑定 a.py 还是 z.py", "dynamic.py"
        )
        self.assertFalse(result.sufficient)
        self.assertIn("同名候选", result.reason)
        candidates = {
            item.path for item in result.evidence
            if item.symbol == "foo"
            and item.metadata.get("resolution") == "ambiguous_candidate"
        }
        self.assertEqual(candidates, {"a.py", "z.py"})

    def test_exact_resolution_replaces_higher_scored_same_span_anchor(self) -> None:
        shared = {
            "path": "a.py", "start_line": 1, "end_line": 2,
            "content": "def foo(value):\n    return 'wrong'",
            "source_hash": self.index.file_record(self.root, "a.py")["source_hash"],
            "language": "python", "relation": "definition", "symbol": "foo",
        }
        anchor = Evidence(**shared, score=2.0, metadata={"resolution": "anchor"})
        exact = Evidence(**shared, score=1.0, metadata={"resolution": "exact"})

        deduped = ContextBroker._dedupe([anchor, exact])

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].metadata["resolution"], "exact")

    def test_project_scoped_conversation_survives_file_switch_without_source_retention(self) -> None:
        store = ConversationStore(ttl_minutes=120, max_sessions=2)
        conversation, created = store.get_or_create(
            None, "browser", "project", "consumer.py")
        self.assertTrue(created)
        store.append(conversation, "tool_result", {
            "content": "SECRET SOURCE BODY",
            "evidence": [{
                "path": "z.py", "start_line": 1, "end_line": 10,
                "source_hash": "abc", "symbol": "foo", "content": "SECRET",
            }],
        })
        same, created = store.get_or_create(
            conversation.conversation_id, "browser", "project", "z.py")
        self.assertFalse(created)
        self.assertIs(same, conversation)
        self.assertEqual(same.active_path, "z.py")
        self.assertEqual(same.anchors[0].symbol, "foo")
        self.assertNotIn("SECRET", json.dumps(same.events[-1].payload))

        other, created = store.get_or_create(
            conversation.conversation_id, "browser", "another-project", "z.py")
        self.assertTrue(created)
        self.assertNotEqual(other.conversation_id, conversation.conversation_id)


if __name__ == "__main__":
    unittest.main()
