import tempfile
import unittest
from pathlib import Path

from app.code_index import CodeIndex
from app.context_broker import ContextBroker
from app.retriever import Retriever

GOLD_TARGETS = {
    "process": "pkg/service.py",
    "Processor": "pkg/service.py",
    "Repository": "pkg/repo.py",
    "normalize": "utilities/mathutil.py",
    "scale": "utilities/mathutil.py",
    "helper": "pkg/service.py",
    "load": "pkg/repo.py",
    "save": "pkg/repo.py",
}


def gold_questions():
    questions = []
    for symbol, path in GOLD_TARGETS.items():
        questions.extend([
            (f"{symbol} 这个符号内部怎么实现", symbol, path),
            (f"请找到 {symbol} 的定义源码", symbol, path),
            (f"谁调用了 {symbol}", symbol, path),
            (f"{symbol} 被修改会影响哪里", symbol, path),
        ])
    return questions


class GoldContextRecallTests(unittest.TestCase):
    """Deterministic 32-question release gate for exact cross-file source recall."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "project"
        (self.root / "pkg").mkdir(parents=True)
        (self.root / "utilities").mkdir()
        (self.root / "pkg" / "__init__.py").write_text(
            "from .service import process\n", encoding="utf-8"
        )
        (self.root / "pkg" / "repo.py").write_text(
            "class Repository:\n"
            "    def load(self, key):\n"
            "        return {'key': key}\n\n"
            "    def save(self, key, value):\n"
            "        return (key, value)\n",
            encoding="utf-8",
        )
        (self.root / "pkg" / "service.py").write_text(
            "from .repo import Repository\n\n"
            "def helper(value):\n"
            "    return value + 1\n\n"
            "class Processor:\n"
            "    def __init__(self):\n"
            "        self.repo = Repository()\n\n"
            "    def run(self, key):\n"
            "        value = self.repo.load(key)\n"
            "        self.repo.save(key, value)\n"
            "        return helper(len(value))\n\n"
            "def process(key):\n"
            "    return Processor().run(key)\n",
            encoding="utf-8",
        )
        (self.root / "utilities" / "mathutil.py").write_text(
            "def normalize(value):\n"
            "    return max(0, min(1, value))\n\n"
            "def scale(value, factor):\n"
            "    return normalize(value) * factor\n",
            encoding="utf-8",
        )
        (self.root / "consumer.py").write_text(
            "from pkg import process as execute\n"
            "from pkg.repo import Repository as Repo\n"
            "from pkg.service import Processor\n"
            "from utilities.mathutil import normalize, scale\n\n"
            "def entry(key):\n"
            "    repo = Repo()\n"
            "    repo.save(key, normalize(1))\n"
            "    return execute(key) + scale(1, 2)\n\n"
            "def build():\n"
            "    return Processor()\n",
            encoding="utf-8",
        )
        self.index = CodeIndex(base / "index.db")
        self.index.index_project(self.root)
        self.broker = ContextBroker(
            self.index, Retriever(self.index, self.root), self.root, repo_map_tokens=900
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_definition_recall_at_one_is_100_percent(self) -> None:
        cases = gold_questions()
        self.assertGreaterEqual(len(cases), 30)
        hits = 0
        false_exact = []
        for question, symbol, expected_path in cases:
            result = self.broker.collect(question, "consumer.py")
            exact = [
                item for item in result.evidence
                if item.relation == "definition"
                and item.symbol == symbol
                and item.metadata.get("resolution") == "exact"
            ]
            if exact and exact[0].path == expected_path:
                hits += 1
            else:
                false_exact.append((question, [item.path for item in exact]))
            self.assertTrue(all(item.validate(self.root) for item in result.evidence))
        self.assertEqual(false_exact, [])
        self.assertEqual(hits / len(cases), 1.0)


if __name__ == "__main__":
    unittest.main()
