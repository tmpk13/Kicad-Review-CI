"""End to end: the GitHub action script, the relay in workerd, the runner, a stand-in
container. Fake services play GitHub's OIDC issuer, the Anthropic API and Apps Script."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import ROOT

BUN = shutil.which("bun")
pytestmark = pytest.mark.skipif(
    not BUN or not (ROOT / "relay/node_modules/miniflare").exists(),
    reason="needs bun and the relay's packages (mise run relay-install)")

SHA = "1808a28aa1c2b3c4d5e6f708192a3b4c5d6e7f80"


@pytest.fixture
def stack():
    assert BUN
    proc = subprocess.Popen([BUN, str(ROOT / "relay/test/serve.ts")], cwd=ROOT / "relay",
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    stdout = proc.stdout
    assert stdout
    info = json.loads(stdout.readline())
    lines: list[str] = []
    threading.Thread(target=lambda: lines.extend(stdout), daemon=True).start()  # keep the pipe drained
    yield info
    assert proc.stdin
    proc.stdin.close()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_push_to_report(stack, tmp_path):
    relay, upstream = stack["relay"], stack["upstream"]
    gh = tmp_path / "gh"
    gh.mkdir()
    action_env = dict(
        os.environ, INPUT_RELAY_URL=relay, INPUT_PATH="oresat-battery-card", INPUT_EFFORT="max",
        INPUT_POLL_SECONDS="0.3", HW_REVIEW_MIN_POLL_SECONDS="0.1",
        ACTIONS_ID_TOKEN_REQUEST_URL=f"{upstream}/oidc?run_id=777001&sha={SHA}",
        ACTIONS_ID_TOKEN_REQUEST_TOKEN="request-token", GITHUB_OUTPUT=str(gh / "output"),
        GITHUB_STEP_SUMMARY=str(gh / "summary.md"), RUNNER_TEMP=str(gh / "temp"))
    action = subprocess.Popen([sys.executable, str(ROOT / "action/review_request.py")], env=action_env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    cfg = tmp_path / "runner.toml"
    jobs = tmp_path / "jobs"
    cfg.write_text(
        f'relay_url = "{relay}"\nrunner_name = "e2e"\nwork_root = "{jobs}"\n'
        "poll_seconds = 0.2\nheartbeat_seconds = 0.3\n"
        f"[podman]\ncommand = [{json.dumps(sys.executable)}, {json.dumps(str(ROOT / 'tests/fake_podman.py'))}]\n")
    runner_env = dict(os.environ, HW_REVIEW_RUNNER_TOKEN="runner-secret-token", FAKE_PODMAN_MODE="ok",
                      FAKE_PODMAN_CALL_API="1", FAKE_PODMAN_STATE=str(tmp_path / "podman-state"))
    deadline = time.time() + 30
    while time.time() < deadline and not (jobs.exists() and any(jobs.iterdir())):
        r = subprocess.run([sys.executable, str(ROOT / "runner/review_runner.py"), "--config", str(cfg), "--once"],
                           env=runner_env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        time.sleep(0.2)

    out, _ = action.communicate(timeout=60)
    assert action.returncode == 0, out
    outputs = dict(line.split("=", 1) for line in (gh / "output").read_text().splitlines())
    assert outputs["status"] == "done"
    assert outputs["drive-url"] == "https://drive.example/uploaded001"
    assert f"`{SHA}`" in Path(outputs["report-path"]).read_text()
    assert "[board-review-1808a28.docx](https://drive.example/uploaded001)" in (gh / "summary.md").read_text()

    jobdir = jobs / outputs["job-id"]
    job = json.loads((jobdir / "work/job.json").read_text())
    assert job["options"]["effort"] == "max" and job["options"]["path"] == "oresat-battery-card"
    # The container reached the Anthropic API through the relay with its job token.
    check = json.loads((jobdir / "out/api-check.json").read_text())
    assert check["status"] == 200 and "Review finished." in check["body"]
    seen = json.loads(urllib.request.urlopen(f"{upstream}/_requests", timeout=10).read())
    to_anthropic = [r for r in seen if r["path"] == "/v1/messages"]
    assert to_anthropic and to_anthropic[-1]["headers"]["x-api-key"] == "sk-ant-real-key"
    # The runner fetched the Drive file before the container started, and uploaded the .docx.
    summary = json.loads((jobdir / "out/summary.json").read_text())
    assert "docs/requirements.md" in summary["drive_seen"]
    uploads = [r for r in seen if r["query"].get("action") == "upload"]
    assert uploads and uploads[0]["query"]["folder"] == "uploadFolder1"
    assert uploads[0]["query"]["name"] == "board-review-1808a28.docx"
