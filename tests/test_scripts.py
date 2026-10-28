import json
import os
from pathlib import Path
import subprocess

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows batch entry point")
@pytest.mark.parametrize(("arguments", "exit_code", "output"), [
    ("tests/test_core.py -q", 0, "passed"),
    ("missing-acceptance-test.py -q", 4, "file or directory not found"),
])
def test_test_batch_locates_project_and_preserves_exit_code(tmp_path, arguments, exit_code, output):
    result = subprocess.run(
        f'cmd.exe /d /s /c ""{ROOT / "test.bat"}" {arguments}"',
        cwd=tmp_path, input="\n", capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert output in result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows batch entry point")
def test_start_batch_rejects_missing_configuration_without_overwriting_env(tmp_path):
    original = (ROOT / ".env").read_bytes()
    env = dict(os.environ, OPENAI_BASE_URL="", OPENAI_MODEL="", OPENAI_API_KEY="")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(ROOT / "start.bat")], cwd=tmp_path, env=env,
        input="\n", capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode != 0
    assert "模型配置无效" in result.stdout + result.stderr
    assert (ROOT / ".env").read_bytes() == original


def test_demo_uses_actual_first_answer_for_second_history(capsys):
    from scripts.demo import run_demo

    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if request.url.path.endswith("extract"):
            return httpx.Response(200, json={"order_id": "A20261028001", "request_type": "refund", "expected_solution": "退款"})
        return httpx.Response(200, text='event: token\ndata: {"content":"A20261028001"}\n\nevent: done\ndata: {"finish_reason":"stop"}\n\n')
    with httpx.Client(base_url="http://local.test", transport=httpx.MockTransport(handler)) as client:
        result = run_demo(client)
    assert seen[1]["history"][1] == {"role": "assistant", "content": "A20261028001"}
    assert result["second_answer"] == "A20261028001"
    assert result["extraction"]["request_type"] == "refund"


@pytest.mark.parametrize("wire", [
    'event: error\ndata: {"message":"upstream error"}\n\n',
    'event: token\ndata: {"content":"partial"}\n\n',
    'event: token\ndata: {"content":"partial"}\n\nevent: done\ndata: {"finish_reason":"length"}\n\n',
])
def test_demo_never_accepts_failed_stream(wire):
    from scripts.demo import DemoFailure, chat

    with httpx.Client(base_url="http://local.test", transport=httpx.MockTransport(lambda request: httpx.Response(200, text=wire))) as client:
        with pytest.raises(DemoFailure):
            chat(client, "hello", [])


def test_field_scoring_distinguishes_null_wrong_type_and_synonyms():
    from scripts.evaluate import score_fields

    expected = {"order_id": [None], "request_type": ["refund"], "expected_solution": ["退款", "退钱"]}
    assert score_fields({"order_id": None, "request_type": "refund", "expected_solution": "退钱"}, expected) == {
        "order_id": True, "request_type": True, "expected_solution": True,
    }
    assert score_fields({"order_id": "None", "request_type": "exchange", "expected_solution": None}, expected) == {
        "order_id": False, "request_type": False, "expected_solution": False,
    }
