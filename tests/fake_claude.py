#!/usr/bin/env python3
"""Stand-in for `claude -p --output-format stream-json` in entrypoint tests.

FAKE_CLAUDE_MODE:
  ok        one tool call, writes $HW_REVIEW_OUT/report.md, ends with success
  budget    writes a partial report, ends with error_max_budget_usd
  textonly  writes no file; the final message carries the review
  slow      keeps working until SIGINT, then ends with error_during_execution
"""

import json
import os
import signal
import sys
import time
from pathlib import Path


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def main() -> int:
    task = sys.stdin.read()
    out = Path(os.environ["HW_REVIEW_OUT"])
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    emit({"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-opus-5-5"})
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Starting."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "kicad-cli sch erc   --format json board.kicad_sch"}}]}})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
    (out / "task-seen.md").write_text(task)
    result = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 2.5,
              "num_turns": 2, "duration_ms": 120000, "session_id": "s1", "result": "Report written."}
    if mode == "ok":
        (out / "report.md").write_text("# Review\n\n**Revision reviewed** - `review` @ `abc`\n")
    elif mode == "budget":
        (out / "report.md").write_text("# Review\n\nP0-1 so far.\n")
        result.update(subtype="error_max_budget_usd", is_error=True, result="")
    elif mode == "textonly":
        result["result"] = "# Review from the final message\n"
    elif mode == "slow":
        stopped = []
        signal.signal(signal.SIGINT, lambda *_: stopped.append(1))
        while not stopped:
            time.sleep(0.05)
        (out / "report.md").write_text("# Review\n\nHalf done.\n")
        result.update(subtype="error_during_execution", is_error=True, result="")
        emit(result)
        return 130
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
