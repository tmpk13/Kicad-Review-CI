#!/usr/bin/env python3
"""Check the finding briefs of a design review report.

A review opens each finding with a brief set as a heading, "#### P0-1. <brief>": one
plain sentence on what is wrong and what it does, which an engineer can follow without
the design open. This script flags briefs that name a reference designator, part
number, pin or net; findings whose brief is bold text instead of a heading; and briefs
well past the 25 words the prompt asks for. The Function, Verdict and First action
lines under "At a glance" get the same name check. Values such as "100 ohm" or "9.6 A"
pass, because a brief may carry a value that states the problem.

The name check is a heuristic built from the tokens earlier reviews put in their
headlines. --allow accepts a word it flags wrongly.

Usage: brief_lint.py REPORT.md [REPORT.md ...] [--allow WORD ...]
Prints "file:line: where: problem" for each problem and exits 1 if there is any.
"""
import argparse
import re
import sys
from pathlib import Path

# The prompt asks for about 25 words; flag only briefs well past that.
MAX_WORDS = 30

# Words that match a name pattern below but name a system or a technology, not a part.
# C3 is the OreSat flight computer card, not a capacitor.
DEFAULT_ALLOW = frozenset({
    "C3", "I2C", "I2S", "USB-C", "DC-DC", "AC-DC", "AEC-Q100", "AEC-Q101", "AEC-Q200",
    "EEPROM", "MOSFET", "MOSFETS", "SERDES", "DXWIFI", "USB2", "USB3", "CANFD", "OPAMP",
    "REACH", "SDRAM", "LPDDR", "SPICE",
    "DDR2", "DDR3", "DDR4", "DDR5", "LPDDR4", "LPDDR5",
})

# Finding IDs (P0-2), priority names (P1) and references to the supporting detail
# (detail S3) would read as designators, so they are blanked before matching.
REFERENCES = re.compile(r"\bP[0-2]-\d+\b|\bP[0-2]\b|\bdetails?\s+S\d+\b", re.I)

# Applied in order; each match is blanked so a later pattern cannot report part of it.
NAME_PATTERNS = [
    # Code spans: net, file and symbol names.
    re.compile(r"`[^`]+`"),
    # Active-low nets: ~SHUTDOWN.
    re.compile(r"~\w+"),
    # A designator with its pin: U7.M15, U2.9.
    re.compile(r"\b[A-Z]{1,3}\d+\.[A-Z]{0,3}\d+\b"),
    # Pin, pad and ball numbers ("pin 3", "ball P7"), but not "a pad 0.1 mm away".
    re.compile(r"\b(?:pins?|pads?|balls?|terminals?)\s+[A-Z]{0,2}\d+\b(?!\.\d)(?!\s*(?:mm|mil|um)\b)", re.I),
    # Names with an underscore: USB1_VBUS, P1_12, gpio3_9.
    re.compile(r"\b[A-Za-z0-9]+_\w+\b"),
    # Power nets (+5V, +3.3V, 5VA), but not values written with a space ("5 V").
    re.compile(r"\+\d+(?:\.\d+)?V\w*|\b\d+V\d*[A-Z]+\b"),
    # Part numbers made only of digits: 73412-0110.
    re.compile(r"\b\d{4,}-\d{3,}\b"),
    # Reference designators: R33, U8, TP12, SW4, FL1.
    re.compile(r"\b(?:ANT|BT|CR|FB|FL|IC|JP|LED|MH|RN|RT|RV|SW|TP|TR|VR|[CDFJKLPQRSTUXYZ])\d{1,4}[A-Z]?\b"),
    # Part numbers: MIC94070YC6, TPS25983, S-8209A, RClamp0504F, 1N4148, nRF52840, and
    # codes of 7 or more capitals and digits with at least 2 digits: TQP3M9037,
    # MTFC32GAZAQHD-IT.
    re.compile(r"\b[A-Z][A-Za-z]{0,8}-?\d{3,}[A-Za-z0-9-]*\b|\b\d[A-Z]\d{3,}[A-Z]*\b"
               r"|\b[a-z][A-Z]{1,3}\d{3,}\w*\b"
               r"|\b(?=[A-Z0-9-]{7,}\b)(?=[A-Z-]*\d[A-Z-]*\d)[A-Z][A-Z0-9-]*\b"),
    # Numbered signals and ports: SYSBOOT2, AIN0, IREF1, IO5, MMC0.
    re.compile(r"\b(?:IO|[A-Z]{3,})\d{1,2}\b"),
    # Hyphenated signal names: BOOT-ROM, CHARGE-DISABLE, EN-UVLO-PK1.
    re.compile(r"\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+\b"),
    # All-capitals words of 5 or more letters are signal names more often than not:
    # MOARPWR, PWRCTL, TRSTN.
    re.compile(r"\b[A-Z]{5,}\b"),
]

HEADING = re.compile(r"^#{2,6}\s+(P[0-2]-\d+)\.\s*(.*?)\s*$")
BOLD_FINDING = re.compile(r"^\s*\*\*(P[0-2]-\d+)\.")
SECTION = re.compile(r"^##\s")
GLANCE = re.compile(r"^##\s+(?:\d+\.\s*)?At a glance\b", re.I)
GLANCE_LINE = re.compile(r"^\s*[-*]\s+\**(Function|Verdict|First action)\**\s*:\**\s*(.*)$", re.I)


def blank(match: re.Match) -> str:
    return " " * len(match.group())


def names(text: str, allow: frozenset[str] = DEFAULT_ALLOW) -> list[str]:
    """Return the designator, part, pin and net names in text, in order of appearance."""
    work = REFERENCES.sub(blank, text)
    for word in allow:
        work = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", blank, work)
    found = []
    for pattern in NAME_PATTERNS:
        found += [(m.start(), m.group()) for m in pattern.finditer(work)]
        work = pattern.sub(blank, work)
    return list(dict.fromkeys(token for _, token in sorted(found)))


def check(text: str, allow: frozenset[str] = DEFAULT_ALLOW) -> list[tuple[int, str, str]]:
    """Return (line number, where, problem) for each problem in a report."""
    problems = []
    in_glance = False
    for n, line in enumerate(text.splitlines(), 1):
        if SECTION.match(line):
            in_glance = bool(GLANCE.match(line))
        if m := HEADING.match(line):
            fid, brief = m.groups()
            if found := names(brief, allow):
                problems.append((n, fid, "names " + ", ".join(found)))
            if (words := len(brief.split())) > MAX_WORDS:
                problems.append((n, fid, f"{words} words; keep the brief under about 25"))
        elif m := BOLD_FINDING.match(line):
            fid = m.group(1)
            problems.append((n, fid, f"brief is bold text; set it as a heading: #### {fid}. <brief>"))
        elif in_glance and (m := GLANCE_LINE.match(line)):
            if found := names(m.group(2), allow):
                problems.append((n, f"At a glance, {m.group(1)}", "names " + ", ".join(found)))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Flag finding briefs that name parts, pins or nets.")
    ap.add_argument("reports", nargs="+", type=Path, help="review report in Markdown")
    ap.add_argument("--allow", action="append", default=[], metavar="WORD",
                    help="accept this word as plain language (repeatable)")
    args = ap.parse_args(argv)
    allow = DEFAULT_ALLOW | set(args.allow)
    count = 0
    for path in args.reports:
        for n, where, problem in check(path.read_text(errors="replace"), allow):
            print(f"{path}:{n}: {where}: {problem}")
            count += 1
    return 1 if count else 0


if __name__ == "__main__":
    sys.exit(main())
