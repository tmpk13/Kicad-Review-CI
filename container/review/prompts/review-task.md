Perform a full PCB design review for the board's next milestone, such as fabrication or first power-on. Use the supplied design files and stated operating conditions. If context is missing, review what you can and identify any decision that depends on it.

Trace power from each input to its loads and critical signals from pin to pin. Cross-check the schematic, layout, BOM, footprints, and manufacturer datasheets where available. Cover startup, ratings, interfaces, return paths, thermal limits, mechanical fit, assembly, and test access as applicable. Report ERC/DRC results only if you ran the checks or inspected their reports.

## 1. At a glance

Start with one line: **Revision reviewed** - `<branch>` @ `<commit SHA>`. Mark any unavailable field as `unknown`. If the reviewed files include uncommitted changes, append `+ local changes`.

In three short lines, state the board's intended function, whether to proceed or hold at the next milestone, and the first action to take. Write them in the same plain language as the finding briefs below, citing finding IDs. If the evidence is insufficient for a verdict, name the missing check.

If an assumption affects the verdict or recommended action, add one short **Assumptions** line naming it and the decision that depends on it. Omit the line if none apply.

## 2. Questions that need answers

Ask only questions whose answers could change the verdict, priority, or fix. Check the supplied files first. Put blocking questions first. For each, say what decision the answer would change.

If a claim needs a datasheet, check the supplied files and, when accessible, the manufacturer's site. If a required datasheet is still missing, add a **Datasheets needed** list here. Use one bullet per missing document. Give the exact part number if known, then name each claim it would verify, including the board reference and relevant specification. Mark those claims as unverified wherever they appear. Omit the list when empty.

## 3. Findings by priority

- **P0 - Blocker** - Resolve before the next milestone. Proceeding risks damage, unsafe operation, or loss of a required function.
- **P1 - Important** - A credible reliability, performance, or assembly issue warrants a fix or targeted verification.
- **P2 - Minor** - A concrete improvement can wait for a later revision.

Open each finding with a brief, set as a heading: `#### P0-1. <brief>`, so the GitHub outline and the Word navigation pane list every finding. The brief is one sentence, under about 25 words, on what is wrong and what it does to the board, which an engineer who has not opened the design can follow. Name circuits by their job ("the PA bias switch", "the boot-mode line"). The brief carries no reference designator, part number, pin or net name; a value may appear when it states the problem ("a 100 ohm resistor where 10 k belongs"). For example: "The power amplifier's switch is enabled from an unconnected pin, so it never transmits", not "U8 (MIC94070YC6) is enabled on a no-connect pin." The body starts with the designators, part numbers and sheet, then gives the board evidence and one specific fix or verification step. Cite the relevant pin, sheet, layer, or datasheet section. If the issue is suspected, say what would confirm it.

Keep each finding's body to two or three lines. Group issues with the same root cause under one brief that names what they share. Include every material finding, omit empty tiers and routine passes, and reserve calculations or longer explanations for a final **Supporting detail** section keyed to the finding. End with one line identifying any significant review gaps.
