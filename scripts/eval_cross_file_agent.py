"""Run real-model, end-to-end cross-file Agent acceptance cases against CodeReader."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


@dataclass
class ChatResult:
    answer: str
    evidence: List[Dict[str, Any]]
    statuses: List[str]
    result: Dict[str, Any]


def _write_fixture(root: Path) -> None:
    files = {
        "a.py": "def transform(value):\n    return f'WRONG_A:{value}'\n",
        "helpers.py": (
            "def normalize(value):\n"
            "    return value.strip().lower()\n"
        ),
        "z.py": (
            "from helpers import normalize\n\n"
            "def transform(value):\n"
            "    cleaned = normalize(value)\n"
            "    return f'RIGHT_Z:{cleaned}'\n"
        ),
        "consumer.py": (
            "from z import transform\n\n"
            "def run(raw):\n"
            "    return transform(raw)\n"
        ),
        "repo.py": (
            "class Repository:\n"
            "    def load(self, key):\n"
            "        return f'DB:{key}'\n"
        ),
        "service.py": (
            "from repo import Repository\n\n"
            "def fetch_user(key):\n"
            "    repo = Repository()\n"
            "    return repo.load(key)\n"
        ),
        "entry.py": (
            "from service import fetch_user\n\n"
            "def main(key):\n"
            "    return fetch_user(key)\n"
        ),
        "pkg/__init__.py": "from .worker import calculate\n",
        "pkg/math_ops.py": "def add_bonus(value):\n    return value + 3\n",
        "pkg/worker.py": (
            "from .math_ops import add_bonus\n\n"
            "def calculate(value):\n"
            "    return add_bonus(value) * 2\n"
        ),
        "use_pkg.py": (
            "from pkg import calculate\n\n"
            "def result(value):\n"
            "    return calculate(value)\n"
        ),
        "dynamic.py": (
            "import a\n"
            "import z\n\n"
            "def choose(module, value):\n"
            "    return module.transform(value)\n"
        ),
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


class CodeReaderClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)
        response = self.client.get("/")
        response.raise_for_status()
        self.headers = {"Origin": self.base_url}

    def close(self) -> None:
        self.client.close()

    def switch_model(self, name: str) -> None:
        response = self.client.post(
            "/api/models/switch", json={"name": name}, headers=self.headers
        )
        response.raise_for_status()
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            health = self.client.get("/api/health").json()
            if health.get("model") == name and (health.get("llama") or {}).get("ready"):
                return
            time.sleep(1)
        raise TimeoutError(f"model did not become ready: {name}")

    def open_project(self, root: Path) -> str:
        response = self.client.post(
            "/api/projects/open", json={"path": str(root.resolve())}, headers=self.headers
        )
        response.raise_for_status()
        return str(response.json()["project_id"])

    def chat(
        self,
        project_id: str,
        relative_path: str,
        question: str,
        conversation_id: Optional[str] = None,
    ) -> ChatResult:
        body: Dict[str, Any] = {
            "project_id": project_id,
            "relative_path": relative_path,
            "question": question,
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        statuses: List[str] = []
        evidence_by_key: Dict[tuple[Any, ...], Dict[str, Any]] = {}
        answer: List[str] = []
        completed: Dict[str, Any] = {}
        with self.client.stream(
            "POST", "/api/chat", json=body, headers=self.headers
        ) as response:
            response.raise_for_status()
            event = "message"
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                envelope = json.loads(line[5:].strip())
                payload = envelope.get("payload") or {}
                event_type = str(envelope.get("type") or event)
                if event_type == "status":
                    state = str(payload.get("state") or "")
                    message = str(payload.get("message") or "")
                    statuses.append(f"{state}: {message}" if message else state)
                elif event_type == "delta":
                    answer.append(str(payload.get("text") or ""))
                elif event_type == "evidence":
                    for item in payload.get("items") or []:
                        key = (
                            item.get("path"), item.get("start_line"), item.get("end_line"),
                            item.get("source_hash"), item.get("relation"), item.get("symbol"),
                        )
                        evidence_by_key[key] = item
                elif event_type == "complete":
                    completed = dict(payload.get("result") or {})
                elif event_type == "error":
                    raise RuntimeError(f"chat error: {payload}")
        if not completed:
            raise RuntimeError("chat stream ended without complete.result")
        return ChatResult("".join(answer), list(evidence_by_key.values()), statuses, completed)


def _paths(result: ChatResult) -> set[str]:
    return {str(item.get("path")) for item in result.evidence}


def _exact_paths(result: ChatResult, symbol: str) -> set[str]:
    return {
        str(item.get("path")) for item in result.evidence
        if item.get("symbol") == symbol
        and (item.get("metadata") or {}).get("resolution") == "exact"
    }


def _contains_all(value: str, terms: List[str]) -> bool:
    folded = value.casefold()
    return all(term.casefold() in folded for term in terms)


def _report(name: str, result: ChatResult, checks: Dict[str, bool]) -> Dict[str, Any]:
    passed = all(checks.values())
    print(f"\n=== {name}: {'PASS' if passed else 'FAIL'} ===")
    print(json.dumps({
        "checks": checks,
        "statuses": result.statuses,
        "stop_reason": result.result.get("stop_reason"),
        "steps_used": result.result.get("steps_used"),
        "tool_protocol": result.result.get("tool_protocol"),
        "paths": sorted(_paths(result)),
        "warnings": result.result.get("warnings") or [],
    }, ensure_ascii=False, indent=2))
    print("ANSWER:\n" + result.answer)
    return {
        "name": name,
        "passed": passed,
        "checks": checks,
        "answer": result.answer,
        "evidence": result.evidence,
        "result": result.result,
        "statuses": result.statuses,
    }


def run(base_url: str, output: Optional[Path], timeout: float,
        model: Optional[str] = None) -> int:
    reports: List[Dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="codereader-agent-eval-") as temporary:
        root = Path(temporary)
        _write_fixture(root)
        api = CodeReaderClient(base_url, timeout)
        try:
            if model:
                api.switch_model(model)
            project_id = api.open_project(root)
            first = api.chat(
                project_id,
                "consumer.py",
                "transform 这个函数内部怎么实现？请明确返回值前缀，不能误用 a.py 的同名函数。",
            )
            reports.append(_report("same_name_import_disambiguation", first, {
                "z_is_only_exact_transform": _exact_paths(first, "transform") == {"z.py"},
                "answer_uses_right_implementation": _contains_all(first.answer, ["RIGHT_Z", "normalize"]),
                "answer_rejects_wrong_sentinel": "WRONG_A" not in first.answer,
                "answer_has_valid_citation": "[E" in first.answer,
                "simple_path_skips_agent": first.result.get("steps_used") == 0,
            }))
            conversation_id = str(first.result["conversation_id"])

            followup = api.chat(
                project_id,
                "z.py",
                "它调用的 normalize 在哪个文件里具体怎么实现？输入 ' MiXeD ' 会得到什么？",
                conversation_id,
            )
            reports.append(_report("project_conversation_file_switch", followup, {
                "conversation_is_preserved": followup.result.get("conversation_id") == conversation_id,
                "helper_source_is_loaded": "helpers.py" in _paths(followup),
                "lower_behavior_is_correct": "mixed" in followup.answer.casefold(),
                "target_result_is_not_conflated_with_caller": "RIGHT_Z:mixed" not in followup.answer,
                "answer_has_valid_citation": "[E" in followup.answer,
            }))

            helper = root / "helpers.py"
            helper.write_text(
                "def normalize(value):\n    return value.strip().upper()\n",
                encoding="utf-8", newline="\n",
            )
            stale = api.chat(
                project_id,
                "z.py",
                "源码刚刚更新了。现在 normalize 输入 ' MiXeD ' 的精确结果是什么？",
                conversation_id,
            )
            reports.append(_report("stale_evidence_reindex", stale, {
                "new_upper_source_is_loaded": any(
                    item.get("path") == "helpers.py" and ".upper()" in str(item.get("content"))
                    for item in stale.evidence
                ),
                "updated_behavior_is_correct": "MIXED" in stale.answer,
                "old_behavior_is_not_asserted": "`mixed`" not in stale.answer,
                "target_result_is_not_conflated_with_caller": "RIGHT_Z:MIXED" not in stale.answer,
                "stale_warning_is_reported": any(
                    "已拒绝" in warning for warning in stale.result.get("warnings") or []
                ),
            }))

            chain = api.chat(
                project_id,
                "entry.py",
                "main 到最终读取数据的两跳调用链是什么？请自行找到并列出每一步路径和实现。",
            )
            reports.append(_report("bounded_agent_two_hop_chain", chain, {
                "research_agent_runs": int(chain.result.get("steps_used") or 0) > 0,
                "read_tools_run": int(chain.result.get("tool_calls_used") or 0) > 0,
                "all_target_files_are_loaded": {"entry.py", "service.py", "repo.py"} <= _paths(chain),
                "answer_has_full_chain": _contains_all(
                    chain.answer, ["main", "fetch_user", "Repository.load", "DB:"]
                ),
                "bounded_steps": int(chain.result.get("steps_used") or 0) <= 3,
                "answer_has_valid_citation": "[E" in chain.answer,
            }))

            relative = api.chat(
                project_id,
                "use_pkg.py",
                "calculate 和它调用的 add_bonus 分别在哪个文件，具体实现和 calculate(4) 的结果是什么？",
            )
            reports.append(_report("relative_import_and_reexport", relative, {
                "both_definitions_are_loaded": {
                    "pkg/worker.py", "pkg/math_ops.py",
                } <= _paths(relative),
                "result_is_fourteen": "14" in relative.answer,
                "answer_has_valid_citation": "[E" in relative.answer,
            }))

            ambiguous = api.chat(
                project_id,
                "dynamic.py",
                "choose 里的 module.transform 实际绑定 a.py 还是 z.py？只根据静态源码回答。",
            )
            unresolved_terms = [
                "无法", "不能", "取决于", "不确定", "运行时", "动态绑定", "由调用者传入",
            ]
            reports.append(_report("dynamic_call_remains_unresolved", ambiguous, {
                "research_agent_runs": int(ambiguous.result.get("steps_used") or 0) > 0,
                "answer_preserves_uncertainty": any(
                    term in ambiguous.answer for term in unresolved_terms
                ),
                "answer_does_not_claim_one_exact_target": not (
                    "实际绑定 a.py" in ambiguous.answer or "实际绑定 z.py" in ambiguous.answer
                ),
                "unsupported_call_chain_not_invented": not any(
                    term in ambiguous.answer for term in ["Repository.load", "repo.py"]
                ),
                "bounded_steps": int(ambiguous.result.get("steps_used") or 0) <= 3,
            }))
        finally:
            api.close()

    summary = {
        "base_url": base_url,
        "passed": sum(1 for report in reports if report["passed"]),
        "total": len(reports),
        "reports": reports,
    }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
    print(f"\nREAL AGENT EVAL: {summary['passed']}/{summary['total']} passed")
    return 0 if summary["passed"] == summary["total"] else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8710")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", help="切换并等待指定本地 GGUF 模型就绪")
    args = parser.parse_args()
    raise SystemExit(run(args.base_url, args.output, args.timeout, args.model))


if __name__ == "__main__":
    main()
