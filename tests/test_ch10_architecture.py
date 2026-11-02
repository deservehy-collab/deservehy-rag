import json
import subprocess
import sys
from pathlib import Path


def test_realtime_path_does_not_import_topic_inference():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import json, sys; import app.main, app.service, app.workflow.graph; "
        "print(json.dumps(sorted(m for m in sys.modules if m in "
        "{'app.topics.model', 'app.topics.service', 'torch', 'transformers'})))"
    )
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=root,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []
