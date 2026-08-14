"""项目级上下文的纯本地行为测试（不依赖模型服务）。"""
import tempfile
import unittest
from pathlib import Path

from app import project_index


class ProjectContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        pkg = self.root / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "aaa_decoy.py").write_text(
            "class Repository:\n"
            '    """不应被子模块别名误命中的同名类。"""\n'
            "    pass\n",
            encoding="utf-8",
        )
        (pkg / "utils.py").write_text(
            '"""输入清洗工具。"""\n\n'
            "def sanitize(value: str) -> str:\n"
            '    """去掉输入首尾空白。"""\n'
            "    return value.strip()\n",
            encoding="utf-8",
        )
        (pkg / "models.py").write_text(
            '"""数据存储层。"""\n'
            "from .utils import sanitize\n\n"
            "class Repository:\n"
            '    """保存清洗后的文本。"""\n\n'
            "    def save(self, value: str) -> str:\n"
            "        cleaned = sanitize(value)\n"
            '        return f"saved:{cleaned}"\n',
            encoding="utf-8",
        )
        (pkg / "service.py").write_text(
            '"""业务编排层。"""\n'
            "from .models import Repository as Storage\n\n"
            "def process(value: str) -> str:\n"
            "    repo = Storage()\n"
            "    return repo.save(value)\n",
            encoding="utf-8",
        )
        (pkg / "facade.py").write_text(
            '"""通过子模块别名对外暴露业务能力。"""\n'
            "from . import models as model_layer\n\n"
            "def persist(value: str) -> str:\n"
            "    return model_layer.Repository().save(value)\n",
            encoding="utf-8",
        )
        (self.root / "cli.py").write_text(
            '"""命令行入口。"""\n'
            "from pkg.service import process\n\n"
            "def main(raw: str) -> str:\n"
            "    return process(raw)\n",
            encoding="utf-8",
        )
        self.index = project_index.build_index(str(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_project_overview_connects_current_imports_and_callers(self) -> None:
        overview = project_index.project_overview(
            self.index, "pkg/service.py", max_chars=4000)

        self.assertIn("pkg/service.py", overview)
        self.assertIn("pkg/models.py", overview)
        self.assertIn("pkg/utils.py", overview)
        self.assertIn("cli.py", overview)
        self.assertIn("依赖", overview)
        self.assertIn("被以下文件引用", overview)

    def test_project_overview_prioritizes_architecture_over_file_dump(self) -> None:
        overview = project_index.project_overview(
            self.index, "pkg/service.py", max_chars=4000)

        self.assertIn("入口点", overview)
        self.assertIn("中心模块", overview)
        self.assertIn("公共 API", overview)
        self.assertNotIn("Python 文件地图", overview)

    def test_related_sources_resolves_alias_method_and_transitive_call(self) -> None:
        code = "repo = Storage()\nreturn repo.save(value)"
        sources = project_index.related_sources(
            self.index,
            code,
            "pkg/service.py",
            str(self.root),
            max_symbols=6,
            max_chars=8000,
            dependency_depth=2,
        )

        self.assertIn("class Repository", sources)
        self.assertIn("def save", sources)
        self.assertIn("def sanitize", sources)
        self.assertIn("pkg/models.py", sources)
        self.assertIn("pkg/utils.py", sources)

    def test_from_import_submodule_alias_resolves_to_project_module(self) -> None:
        sources = project_index.related_sources(
            self.index,
            "return model_layer.Repository().save(value)",
            "pkg/facade.py",
            str(self.root),
            max_symbols=6,
            max_chars=8000,
        )

        self.assertIn("class Repository", sources)
        self.assertIn("pkg/models.py", sources)
        self.assertNotIn("pkg/aaa_decoy.py", sources)

    def test_current_definition_includes_upstream_caller_context(self) -> None:
        sources = project_index.related_sources(
            self.index,
            "def process(value: str) -> str:\n    pass",
            "pkg/service.py",
            str(self.root),
            max_symbols=5,
            max_chars=6000,
        )

        self.assertIn("cli.py", sources)
        self.assertIn("调用方", sources)
        self.assertIn("def main", sources)

    def test_project_context_contains_map_and_real_sources(self) -> None:
        context = project_index.build_project_context(
            self.index,
            "Storage().save(value)",
            "pkg/service.py",
            str(self.root),
            question="这段调用在整个项目中处于什么位置？",
            max_chars=9000,
        )

        self.assertIn("项目全貌", context)
        self.assertIn("关联源码", context)
        self.assertIn("class Repository", context)
        self.assertLessEqual(len(context), 9000)


if __name__ == "__main__":
    unittest.main()
