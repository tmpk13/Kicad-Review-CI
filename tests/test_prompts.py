"""The screenshot prompt is the base review prompt plus a Screenshots section. It is kept
as a full copy so it can be handed to a session as one file, which means an edit to the
base prompt has to be copied into it by hand; these tests catch a missed copy."""

from conftest import ROOT

PROMPTS = ROOT / "container/review/prompts"


def missing_lines(base: list[str], variant: list[str]) -> list[str]:
    """Lines of `base` that do not appear in `variant` in the same order (a subsequence
    check: the variant may add lines anywhere but must keep every base line)."""
    missing = []
    pos = 0
    for line in base:
        try:
            pos = variant.index(line, pos) + 1
        except ValueError:
            missing.append(line)
    return missing


def test_missing_lines_finds_dropped_and_reordered_lines():
    assert missing_lines(["a", "b", "c"], ["a", "x", "b", "y", "c"]) == []
    assert missing_lines(["a", "b", "c"], ["a", "c"]) == ["b"]
    assert missing_lines(["a", "b"], ["b", "a"]) == ["b"]


def test_screenshot_prompt_keeps_the_whole_base_prompt():
    base = (PROMPTS / "review-task.md").read_text().splitlines()
    variant = (PROMPTS / "review-task-screenshots.md").read_text().splitlines()
    assert missing_lines(base, variant) == [], "copy the review-task.md change into review-task-screenshots.md"


def test_prompts_are_ascii():
    for path in PROMPTS.glob("*.md"):
        text = path.read_text()
        assert text.isascii(), f"{path.name} has non-ASCII characters"
