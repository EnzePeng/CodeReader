"""提示词应把项目级上下文贯穿总览、分段与追问。"""
import unittest
from unittest.mock import patch

from app import explainer

SEGMENT = {
    "id": "s1",
    "kind": "function",
    "title": "函数 run",
    "start_line": 10,
    "end_line": 11,
    "code": "def run():\n    return service.execute()",
}
PROJECT_CONTEXT = (
    "## 项目全貌\n- app.py 依赖 service.py\n\n"
    "## 关联源码\n### execute —— service.py:5\n```python\ndef execute(): ...\n```"
)


class ExplainerProjectContextTest(unittest.TestCase):
    def test_overview_prompt_contains_project_position(self) -> None:
        messages = explainer.build_overview_messages(
            "app.py", SEGMENT["code"], [SEGMENT], "python", PROJECT_CONTEXT)
        prompt = messages[-1]["content"]
        self.assertIn("项目全貌", prompt)
        self.assertIn("项目中的职责", prompt)

    def test_segment_prompt_requires_cross_file_relationship_explanation(self) -> None:
        messages = explainer.build_segment_messages(
            "app.py", "应用入口", "", SEGMENT, "python", PROJECT_CONTEXT)
        prompt = messages[-1]["content"]
        self.assertIn("项目全貌", prompt)
        self.assertIn("上游调用", prompt)
        self.assertIn("下游依赖", prompt)

    def test_chat_prompt_uses_project_context_without_selection(self) -> None:
        messages = explainer.build_chat_messages(
            "app.py", "应用入口", None, None, [], "项目的数据如何流动？",
            "python", PROJECT_CONTEXT, "def run():\n    return service.execute()")
        prompt = messages[1]["content"]
        self.assertIn("项目全貌", prompt)
        self.assertIn("用户没有选中具体代码", prompt)
        self.assertIn("def run", prompt)
        self.assertIn("即使用户没有选中代码", messages[0]["content"])

    def test_cache_keys_change_with_project_context(self) -> None:
        with patch.object(explainer, "model_id", return_value="test-model"), \
                patch.object(explainer, "_think_tag", return_value="t0"):
            self.assertNotEqual(
                explainer.overview_key("file-hash", "project-a"),
                explainer.overview_key("file-hash", "project-b"),
            )
            self.assertNotEqual(
                explainer.segment_key(SEGMENT, "simple", "project-a"),
                explainer.segment_key(SEGMENT, "simple", "project-b"),
            )


if __name__ == "__main__":
    unittest.main()
