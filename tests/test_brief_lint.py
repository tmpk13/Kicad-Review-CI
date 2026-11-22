import pytest

from conftest import load

lint = load("brief_lint", "container/review/tools/brief_lint.py")


# Headlines from the OreSat Live and battery card reviews written before the brief rule.
@pytest.mark.parametrize("headline, expected", [
    ("U8 (MIC94070YC6) is enabled on a no-connect pin, so the PA bias never turns on and the card "
     "cannot transmit at power.", ["U8", "MIC94070YC6"]),
    ("USB1_VBUS (U7.M15) and the hub VBUS detect (U2.9) have no 5 V source, so the hub never attaches: "
     "no camera, no WiFi.", ["USB1_VBUS", "U7.M15", "U2.9"]),
    ("SYSBOOT2 is on MAX7310 IO0, but the C3 software drives it on IO5 and drives IO0 low, so under C3 "
     "control the SiP never tries the eMMC.", ["SYSBOOT2", "MAX7310", "IO0", "IO5"]),
    ("R33 is 100 ohm, so MOARPWR does not raise the card breaker limit and running the heaters latches "
     "the card off.", ["R33", "MOARPWR"]),
    ("`~SHUTDOWN` has about 4 uA of margin: roughly 2 Mohm to ground anywhere on the line starts to open "
     "the fullest pack's upper cell switch.", ["`~SHUTDOWN`"]),
    ("The C3 drives BOOT-ROM (MAX7310 IO5) high for a normal start, which on this card selects the ROM "
     "bootloader.", ["BOOT-ROM", "MAX7310", "IO5"]),
    ("WiFi PA sheet: LNA_SHUTDOWN drives U8 pad 6, drawn as EN; pad 3 is open.",
     ["LNA_SHUTDOWN", "U8", "pad 6", "pad 3"]),
    ("U7's footprint has a 4.4 mm ground pad; the MCXN947VKLT package (SOT2229-1) has a 5.1 to 6.9 mm "
     "exposed pad.", ["U7", "MCXN947VKLT", "SOT2229-1"]),
    ("An OSD3358 drives a TQP3M9037 LNA, the BOM lists MTFC32GAZAQHD-IT, and an nRF52840 idles.",
     ["OSD3358", "TQP3M9037", "MTFC32GAZAQHD-IT", "nRF52840"]),
    ("No test pad on +5V, 5VA or +3.3V; the Molex 73412-0110 pattern and TRSTN are wrong.",
     ["+5V", "5VA", "+3.3V", "73412-0110", "TRSTN"]),
])
def test_old_headlines_are_flagged(headline, expected):
    assert lint.names(headline) == expected


@pytest.mark.parametrize("brief", [
    "The PA bias switch is enabled through a no-connect pin, so the PA never turns on and the card "
    "cannot transmit at full power.",
    "The board and the C3 software disagree on which line sets the boot mode, so under C3 control the "
    "card never boots from eMMC.",
    "A 100 ohm resistor where 10 k belongs stops the high-power request from raising the breaker limit, "
    "so running the heaters latches the card off.",
    "The pack eFuses are recommended only down to -15 C, but the packs must discharge at -20 C.",
    "The board fails DRC: a ground via sits 0.101 mm from a pad, under the 0.127 mm fab minimum.",
    "The C3's watchdog line has 4 uA of margin; 2 Mohm to ground anywhere opens a cell switch "
    "(P1-3, detail S3).",
    "The 12 pF crystal capacitors leave the I2C and USB-C clocks 100 ppm fast; see P2.",
])
def test_plain_briefs_and_values_pass(brief):
    assert lint.names(brief) == []


REPORT = """# Board review

## 1. At a glance

**Revision reviewed** - `master` @ `abc1234`.

- Function: a Linux computer streams a camera over WiFi.
- Verdict: Hold (P0-1).
- First action: restore the PA bias enable on U8 pin 3 (P0-1).

## 2. Questions that need answers

1. Which eMMC: MTFC32GAZAQHD-IT or THGBMJG8C4LBAU8?

## 3. Findings by priority

### P0 - Blocker

#### P0-1. The PA bias switch is enabled through a no-connect pin, so the card cannot transmit.
U8 (MIC94070YC6), WiFi PA sheet: LNA_SHUTDOWN drives pad 6.

**P1-1. U2 hub straps disable port power management.**
Pin 36 is open.

#### P2-1. The crystals have too little load capacitance, so every clock in the system runs fast, which upsets the USB timing, the CAN bit timing and the real-time clock alike on every board.
"""


def test_check_covers_briefs_layout_length_and_at_a_glance():
    # Bodies, the revision line and the questions may name parts; only briefs and the
    # three At a glance lines are checked.
    assert lint.check(REPORT) == [
        (9, "At a glance, First action", "names U8, pin 3"),
        (22, "P1-1", "brief is bold text; set it as a heading: #### P1-1. <brief>"),
        (25, "P2-1", "32 words; keep the brief under about 25"),
    ]


def test_main_prints_problems_and_exit_status(tmp_path, capsys):
    report = tmp_path / "report.md"
    report.write_text(REPORT)
    assert lint.main([str(report)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"{report}:9: At a glance, First action: names U8, pin 3"
    assert len(out) == 3
    assert lint.main([str(report), "--allow", "U8"]) == 1
    assert capsys.readouterr().out.splitlines()[0].endswith("First action: names pin 3")
    clean = tmp_path / "clean.md"
    clean.write_text("#### P0-1. The card breaker can never trip, so the card has no overcurrent protection.\n")
    assert lint.main([str(clean)]) == 0
    assert capsys.readouterr().out == ""
