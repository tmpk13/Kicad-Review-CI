#!/usr/bin/env python3
"""Stand-in for `podman` in runner tests: plays the review container's side of the contract.

`run` reads job.json from the /work mount and writes report.md, report.docx and
summary.json into the /out mount, the way the real entrypoint does. FAKE_PODMAN_MODE:
  ok    write a complete review (default)
  fail  write a failure summary and exit 1
  hang  write progress, then wait until `podman stop`
With FAKE_PODMAN_CALL_API=1, `run` also calls the Anthropic API through
ANTHROPIC_BASE_URL with ANTHROPIC_API_KEY and records the answer in api-check.json.
"""

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("FAKE_PODMAN_STATE", "/tmp/fake-podman"))


def mounts(args: list[str]) -> dict[str, Path]:
    out = {}
    for i, a in enumerate(args):
        if a == "-v":
            src, dst = args[i + 1].split(":")[:2]
            out[dst] = Path(src)
    return out


def run(args: list[str]) -> int:
    name = args[args.index("--name") + 1]
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"{name}.pid").write_text(str(os.getpid()))
    (STATE / f"{name}.args").write_text(json.dumps(args))
    m = mounts(args)
    work, out = m["/work"], m["/out"]
    job = json.loads((work / "job.json").read_text())
    mode = os.environ.get("FAKE_PODMAN_MODE", "ok")
    (out / "progress.json").write_text(json.dumps({"phase": "review", "message": "Bash: kicad-cli sch erc", "turns": 3}))
    if os.environ.get("FAKE_PODMAN_CALL_API") == "1":
        req = urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages", method="POST",
                                     data=b'{"model":"claude-opus-5-5","max_tokens":5,"messages":[]}',
                                     headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "content-type": "application/json",
                                              "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                check = {"status": resp.status, "body": resp.read().decode()[:2000]}
        except urllib.error.HTTPError as e:
            check = {"status": e.code, "body": e.read().decode()[:2000]}
        (out / "api-check.json").write_text(json.dumps(check))
    if mode == "hang":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        while True:
            time.sleep(0.2)
    if mode == "fail":
        (out / "summary.json").write_text(json.dumps({"ok": False, "error": "kicad-cli crashed"}))
        print("fake container: failing on purpose")
        return 1
    drive = sorted(str(p.relative_to(work / "drive")) for p in (work / "drive").rglob("*") if p.is_file())
    (out / "report.md").write_text(f"# Review\n\n**Revision reviewed** - `{job['ref']}` @ `{job['sha']}`\n")
    (out / "report.docx").write_bytes(b"PK\x03\x04fake-docx")
    (out / "summary.json").write_text(json.dumps({
        "ok": True, "complete": True, "error": None, "report_name": f"board-review-{job['sha'][:7]}",
        "claude": {"subtype": "success", "total_cost_usd": 1.25, "num_turns": 7, "duration_ms": 60000},
        "drive_seen": drive}))
    print("fake container: review written")
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "run":
        return run(args)
    if cmd in ("stop", "kill"):
        name = args[-1]
        pid_file = STATE / f"{name}.pid"
        (STATE / f"{name}.{cmd}").write_text("1")
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM if cmd == "stop" else signal.SIGKILL)
            except ProcessLookupError:
                pass
        return 0
    if cmd in ("ps", "rm"):
        return 0
    print(f"fake podman: unsupported command {args}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
