import json
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT, load

ep = load("entrypoint", "container/review/bin/entrypoint.py")
FIXTURES = ROOT / "tests/fixtures"

JOB = {
    "id": "0e7c0d5e-1111-4222-8333-944455556666", "repo": "oresat/oresat-batteries", "ref": "refs/heads/review",
    "sha": "1808a28aa1c2b3c4d5e6f708192a3b4c5d6e7f80", "event": "push", "actor": "octocat", "run_id": "42",
    "options": {"path": "oresat-battery-card", "model": "claude-opus-5-5", "effort": "high", "budget_usd": 20,
                "max_runtime_minutes": 180},
}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    work, out = tmp_path / "work", tmp_path / "out"
    work.mkdir()
    out.mkdir()
    monkeypatch.setattr(ep, "WORK", work)
    monkeypatch.setattr(ep, "OUT", out)
    monkeypatch.setattr(ep, "OPT", ROOT / "container/review")
    monkeypatch.setenv("HW_REVIEW_OUT", str(out))
    return work, out


def test_erc_counts_and_ignored_checks():
    c = ep.count_violations(FIXTURES / "erc.json")
    assert c["violations"]["total"] == 6
    assert "power_pin_not_driven" in c["ignored_checks"]
    text = ep.describe_counts(c)
    assert "checks ignored by the project: power_pin_not_driven" in text
    assert "label_dangling" in text


def test_drc_counts_groups():
    c = ep.count_violations(FIXTURES / "drc.json")
    assert c["violations"]["by_severity"] == {"error": 3, "warning": 4}
    assert c["unconnected_items"]["total"] == 0
    assert "schematic_parity" in c
    assert "3 error, 4 warning; 0 unconnected items; 0 schematic parity" in ep.describe_counts(c)
    assert ep.count_violations(FIXTURES / "missing.json") is None


def test_library_vars_skip_kicad_builtins(tmp_path):
    (tmp_path / "sym-lib-table").write_text(
        '(sym_lib_table (lib (name "a")(uri "${ORESAT_LIBRARIES}/oresat-symbols/a.kicad_sym"))'
        '(lib (name "b")(uri "${KICAD10_SYMBOL_DIR}/Device.kicad_sym"))(lib (name "c")(uri "${KIPRJMOD}/c.kicad_sym")))')
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/fp-lib-table").write_text('(fp_lib_table (lib (name "x")(uri "${MY_LIBS}/x.pretty")))')
    assert ep.library_vars(tmp_path) == {"ORESAT_LIBRARIES", "MY_LIBS"}
    got = ep.clone_libraries({"MY_LIBS"}, {}, tmp_path / "libs")
    assert "no repository is known" in got["MY_LIBS"]["error"]


def test_find_projects_skips_backups_and_hidden(tmp_path):
    for p in ("board/board.kicad_pro", "board/board-backups/old.kicad_pro", ".git/x.kicad_pro", "mli/mli.kicad_pro"):
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text("{}")
    assert [p.relative_to(tmp_path).as_posix() for p in ep.find_projects(tmp_path)] == ["board/board.kicad_pro", "mli/mli.kicad_pro"]


def test_prompt_override_and_drive_files(tmp_path):
    (tmp_path / "Battery card").mkdir()
    (tmp_path / "Battery card/review-prompt.md").write_text("Custom")
    (tmp_path / "_drive-manifest.json").write_text("[]")
    assert ep.prompt_override(tmp_path) == tmp_path / "Battery card/review-prompt.md"
    assert [p.name for p in ep.drive_files(tmp_path)] == ["review-prompt.md"]
    assert ep.prompt_override(tmp_path / "nothing") is None


def task_ctx(**over):
    ctx = {"projects": ["oresat-battery-card/oresat-batteries.kicad_pro"], "checks_line": "ERC 3 error",
           "commit_message": "Add BOOT-ROM inverter", "drive_count": 2, "earlier_reviews": ["Battery card/rev1-review.docx"],
           "libraries": {"ORESAT_LIBRARIES": {"path": "/work/libs/ORESAT_LIBRARIES", "url": "u", "commit": "a206ef6"}},
           "team_context": "/opt/review/prompts/context-oresat.md", "minutes": 170, "prompt_source": "built-in"}
    ctx.update(over)
    return ctx


def test_build_task_states_revision_limits_and_context():
    task = ep.build_task(JOB, "INSTRUCTIONS", task_ctx())
    assert task.startswith("INSTRUCTIONS\n\n## This review")
    assert f"**Revision reviewed** - `review` @ `{JOB['sha']}`" in task
    assert "about $20 of API spend and 170 minutes" in task
    assert "Earlier reviews in the shared drive" in task
    assert "context-oresat.md" in task
    assert "https://github.com/oresat/oresat-batteries/actions/runs/42" in task
    bare = ep.build_task(JOB, "I", task_ctx(drive_count=0, earlier_reviews=[], team_context=None, projects=[],
                                            libraries={"X": {"error": "clone failed"}}))
    assert "Shared drive: no files" in bare and "No KiCad project" in bare and "`X`: not resolved: clone failed" in bare


def test_claude_command_uses_job_options():
    cmd = ep.claude_command(JOB)
    assert cmd[:2] == ["claude", "-p"] and "--bare" not in cmd
    assert "Workflow" in cmd[cmd.index("--disallowedTools") + 1]
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "20.00"
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"


def run_fake(mode: str, work: Path, monkeypatch, deadline_s: float = 60) -> dict:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    import os
    return ep.run_claude([sys.executable, str(ROOT / "tests/fake_claude.py")], "THE TASK", time.time() + deadline_s, dict(os.environ))


def test_run_claude_streams_progress_and_result(dirs, monkeypatch):
    work, out = dirs
    state = run_fake("ok", work, monkeypatch)
    assert state["exit"] == 0 and state["stopped"] is None
    assert state["result"]["total_cost_usd"] == 2.5
    assert state["tools"] == 1
    assert (out / "task-seen.md").read_text() == "THE TASK"
    assert len((out / "transcript.jsonl").read_text().splitlines()) == 4
    assert json.loads((out / "progress.json").read_text())["message"] == "Bash: kicad-cli sch erc --format json board.kicad_sch"


def test_run_claude_stops_at_the_deadline(dirs, monkeypatch):
    work, out = dirs
    started = time.time()
    state = run_fake("slow", work, monkeypatch, deadline_s=1)
    assert time.time() - started < 20
    assert state["stopped"] == "time limit reached"
    assert state["result"]["subtype"] == "error_during_execution"
    has, complete, error = ep.finalize_report(out / "report.md", state, ["## Review run"])
    assert (has, complete) == (True, False)
    text = (out / "report.md").read_text()
    assert text.startswith("> **Incomplete review**") and "time limit reached" in text
    assert text.rstrip().endswith("## Review run")


@pytest.mark.parametrize("mode,has,complete,first", [
    ("ok", True, True, "# Review"),
    ("budget", True, False, "> **Incomplete review**"),
    ("textonly", True, True, "> Claude Code ended without writing"),
])
def test_finalize_report(dirs, monkeypatch, mode, has, complete, first):
    work, out = dirs
    state = run_fake(mode, work, monkeypatch)
    got = ep.finalize_report(out / "report.md", state, ["## Review run", "", "| | |"])
    assert got[:2] == (has, complete)
    text = (out / "report.md").read_text()
    assert text.startswith(first)
    assert "\n---\n\n## Review run" in text


def test_finalize_without_any_output(dirs):
    _, out = dirs
    has, complete, error = ep.finalize_report(out / "report.md", {"result": None, "exit": 1, "stopped": None}, [])
    assert (has, complete) == (False, False)
    assert error == "no report: Claude Code exited (exit 1)"
    assert not (out / "report.md").exists()


def test_run_record_and_report_name():
    state = {"result": {"total_cost_usd": 3.456, "num_turns": 50, "duration_ms": 3_000_000}, "turns": 50}
    rows = ep.run_record(JOB, state, task_ctx(), {"claude": "2.1.323", "kicad": "10.0.8"})
    text = "\n".join(rows)
    assert "| Spend | $3.46 of $20.00, 50 turns, 50 min |" in text
    assert "Claude Code 2.1.323, KiCad 10.0.8" in text
    name = ep.report_name(JOB, ["oresat-battery-card/oresat-batteries.kicad_pro"])
    assert name.startswith("oresat-batteries-review-") and name.endswith("-1808a28")
    assert ep.report_name(JOB, ["a/x.kicad_pro", "b/y.kicad_pro"]).startswith("oresat-batteries-review-")


def test_describe_tool_prefers_the_useful_field():
    assert ep.describe_tool({"name": "Read", "input": {"file_path": "/work/repo/a.kicad_sch"}}) == "Read: /work/repo/a.kicad_sch"
    assert ep.describe_tool({"name": "TodoWrite", "input": {"todos": []}}) == "TodoWrite"
