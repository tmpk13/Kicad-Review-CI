Perform a full PCB design review for the board's next milestone, such as fabrication or first power-on. Use the supplied design files and stated operating conditions. If context is missing, review what you can and identify any decision that depends on it.

Trace power from each input to its loads and critical signals from pin to pin. Cross-check the schematic, layout, BOM, footprints, and manufacturer datasheets where available. Cover startup, ratings, interfaces, return paths, thermal limits, mechanical fit, assembly, and test access as applicable. Report ERC/DRC results only if you ran the checks or inspected their reports.

Back each finding whose evidence is on the board with a screenshot of that area, plotted with only the layers that show the problem. **Screenshots** at the end says how to make them.

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

When a finding's evidence is on the board (placement, routing, copper, spacing, mask, mechanical fit, test access), follow its body with one screenshot, or two when the problem lies between two views, such as a track and the plane beneath it. A finding that rests only on the schematic, BOM or a datasheet gets none. Under each image put a one-line italic caption: the side, each layer and the color it shows in, which pours are hidden, and what the magenta box marks. Captions may name designators and nets.

```markdown
![P1-2](CLD-REVIEW-BATTERY-4-shots/P1-2.png)
*Top side: inner ground plane (green) under top copper (red), top pours hidden; magenta box: U3.*
```

## Screenshots

This section says how to make the images; it is not a section of the report.

### Where they go

Put the images in a folder beside the report, named after it with `-shots` appended (`CLD-REVIEW-BATTERY-4-shots/`). Name each by its finding ID: `P1-2.png`, and `P1-2b.png` for a second view. Rename the files when findings are renumbered. Links are relative to the report, so the folder moves with it; to make a Word copy, run pandoc from the report's folder or pass `--resource-path`.

### Choose the layers

Show the layers that carry the evidence and nothing else. Layers paint in the order given, so list planes first and fab or silkscreen last. For the back side use the `B.*` layers and `--bottom`, which shows the board as seen from below, with its text reading the right way.

| The finding is about | `--layers` | Other options |
|---|---|---|
| Placement, orientation, polarity or pin-1 marks | `F.Fab,F.Silkscreen` | |
| The distance between parts, such as a capacitor far from its pin | `F.Cu,F.Silkscreen` | one `--mark-area` per part |
| A track's route, or a missing or wrong connection | each copper layer the net uses, then `F.Fab` | `--clear-fill` those layers, `--nets` the net |
| A return path: a signal over a plane gap or split, missing stitching vias | the plane layer, the signal layer, `F.Fab` | `--clear-fill` the signal layer, `--nets` the signal |
| A signal over the wrong pour, such as a supply plane | the plane layer, the signal layer, `F.Fab` | as above, plus `--fill-nets` that pour's net |
| Copper for heat or current: pour size, thermal vias, neck-downs | the copper layers that carry it, `F.Fab` | |
| Spacing between nets: clearance, creepage | the copper layer involved, alone | |
| Solder mask: slivers, missing openings, bridges | `F.Mask,F.Cu,F.Fab` | |
| Solder paste: apertures on exposed pads, paste on vias | `F.Cu,F.Paste,F.Fab` | |
| Mechanical fit: board edge, holes, connector overhang, keepouts | `F.Courtyard,F.Fab` and the layer with the mechanical drawing, often `User.Drawings` | |
| Test points, debug access, labels | `F.Fab,F.Silkscreen` | |

- Add `Edge.Cuts` whenever the board edge or a cutout falls inside the crop.
- `F.Fab` carries pad outlines and pad numbers, and the script drops value text; use it to show which pin a track reaches. Its outlines paint over the copper and hide gaps under about 0.2 mm, so leave it out of spacing images and name the pads in the caption. Add `F.Silkscreen` when the reader needs designators to find their way, or when the finding is about the silkscreen; its references read better than the small ones on `F.Fab`.
- A filled pour paints over every track on its layer. Hide it with `--clear-fill` when the finding is about tracks; keep it when the finding is about a plane, copper area or spacing.
- All pours on one layer share its color, so a split plane looks like one sheet. When the finding names the pour beneath a track ("over the PA supply"), keep only that pour with `--fill-nets`; when it is about voids or splits, keep them all.
- On a busy layer, `--nets` keeps only the finding's tracks and vias; pads and pours stay. Both options take glob patterns of net names (`'/USB HUB/WIFI_USB_*'`).
- Put at most three copper layers in one image. When more are involved, make one image per layer with the same `--area`.
- Plots use a dark ground, on which the default layer colors read best. `B.Fab` is dim on it: add `--light` when back-side pad numbers matter.
- Colors for the caption: F.Cu red, In1.Cu green, In2.Cu orange, B.Cu blue, F.Fab grey, B.Fab slate, F.Silkscreen yellow, B.Silkscreen pink, F.Mask violet, F.Paste taupe, Edge.Cuts light grey, drill holes white or black, marks magenta.

### Plot and crop

Save this script once per review in your scratch directory. It needs KiCad 10 (`kicad-cli` and its `pcbnew` Python module) and poppler (`pdftoppm`, `pdfinfo`). It refills every pour so the picture matches the zone outlines rather than a stale saved fill, plots the chosen layers from a copy of the board, and crops the plot to the parts or area given. The board file is never written.

```python
"""Plot chosen layers of a KiCad board and crop the plot around parts into a PNG.

python3 pcb_shot.py BOARD OUT.png --layers In1.Cu,F.Cu,F.Fab --around U3,R12
    [--area X0,Y0,X1,Y1] [--mark U3] [--mark-area X0,Y0,X1,Y1]... [--clear-fill F.Cu]
    [--nets 'NAME,/SHEET/USB_*'] [--fill-nets 5VA] [--margin 3] [--bottom] [--light] [--px 1400]

Layers are painted in the order given, so list planes first and fab or silkscreen last.
Coordinates are board millimeters as the PCB editor shows them. The board file is
never written; the plot comes from a copy in a temporary directory.
"""
import argparse
import fnmatch
import subprocess
import tempfile
from pathlib import Path

import pcbnew


def box_mm(board, refs):
    """Union of the named footprints' bounding boxes, text excluded: (x0, y0, x1, y1) in mm."""
    boxes = []
    for ref in refs:
        fp = board.FindFootprintByReference(ref)
        if fp is None:
            raise SystemExit(f"no footprint {ref} on the board")
        r = fp.GetBoundingBox(False)
        boxes.append([pcbnew.ToMM(v) for v in (r.GetLeft(), r.GetTop(), r.GetRight(), r.GetBottom())])
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def area(text):
    x0, y0, x1, y1 = (float(v) for v in text.split(","))
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def main():
    ap = argparse.ArgumentParser(description="Plot chosen board layers and crop them into a PNG.")
    ap.add_argument("board")
    ap.add_argument("out")
    ap.add_argument("--layers", required=True, help="comma separated, bottom of the stack first")
    ap.add_argument("--around", default="", help="references the crop must contain")
    ap.add_argument("--area", type=area, help="crop box in mm, instead of or added to --around")
    ap.add_argument("--mark", default="", help="references to outline in magenta")
    ap.add_argument("--mark-area", type=area, action="append", default=[],
                    help="box in mm to outline in magenta; repeat for several")
    ap.add_argument("--clear-fill", default="", help="copper layers whose pours are hidden")
    ap.add_argument("--nets", default="", help="draw only these nets' tracks and vias (glob patterns)")
    ap.add_argument("--fill-nets", default="", help="draw only these nets' pours (glob patterns)")
    ap.add_argument("--margin", type=float, default=3.0, help="mm added around the crop")
    ap.add_argument("--bottom", action="store_true", help="view from below (mirrored), for B.* layers")
    ap.add_argument("--light", action="store_true", help="light ground: B.Fab reads better on it")
    ap.add_argument("--px", type=int, default=1400, help="pixels on the image's long side")
    a = ap.parse_args()

    board = pcbnew.LoadBoard(a.board)
    if any(name.endswith(".Cu") for name in a.layers.split(",")):
        # Refill every pour so the picture matches the zone outlines, not a stale saved fill.
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    hide = {board.GetLayerID(name) for name in a.clear_fill.split(",") if name}
    fills = [k for k in a.fill_nets.split(",") if k]
    for zone in board.Zones():
        other = fills and not any(fnmatch.fnmatchcase(zone.GetNetname(), k) for k in fills)
        for layer in zone.GetLayerSet().Seq():
            if layer in hide or other:
                # Per layer: one zone can span several copper layers.
                zone.SetFilledPolysList(layer, pcbnew.SHAPE_POLY_SET())

    if a.nets:
        # Pads and pours stay; tracks and vias of every other net are dropped from the copy.
        keep = a.nets.split(",")
        for t in list(board.GetTracks()):
            if not any(fnmatch.fnmatchcase(t.GetNetname(), k) for k in keep):
                # Delete, not Remove: after Remove the KiCad 10 bindings fail in GetDrawings().
                board.Delete(t)

    boxes = [box_mm(board, a.around.split(","))] if a.around else []
    boxes += [a.area] if a.area else []
    if not boxes:
        raise SystemExit("give --around or --area")
    x0 = min(b[0] for b in boxes) - a.margin
    y0 = min(b[1] for b in boxes) - a.margin
    x1 = max(b[2] for b in boxes) + a.margin
    y1 = max(b[3] for b in boxes) + a.margin

    layers = a.layers
    if "Edge.Cuts" in layers.split(","):
        # Outline lines are often hairlines that vanish at whole-board scale; widen them.
        least = pcbnew.FromMM(max(x1 - x0, y1 - y0) / 400)
        shapes = list(board.GetDrawings()) + [g for fp in board.GetFootprints() for g in fp.GraphicalItems()]
        for g in shapes:
            if g.GetLayer() == pcbnew.Edge_Cuts and g.GetWidth() < least:
                g.SetWidth(least)
    marks = ([box_mm(board, a.mark.split(","))] if a.mark else []) + a.mark_area
    if marks:
        # Line width and gap scale with the crop, so a mark reads the same at every zoom.
        pen = max(x1 - x0, y1 - y0) / 150
        # The Margin layer plots in magenta by default; empty it so it carries only the marks.
        for item in [d for d in board.GetDrawings() if d.GetLayer() == pcbnew.Margin]:
            board.Delete(item)
        for mx0, my0, mx1, my1 in marks:
            rect = pcbnew.PCB_SHAPE(board)
            rect.SetShape(pcbnew.SHAPE_T_RECT)
            rect.SetStart(pcbnew.VECTOR2I_MM(mx0 - pen, my0 - pen))
            rect.SetEnd(pcbnew.VECTOR2I_MM(mx1 + pen, my1 + pen))
            rect.SetLayer(pcbnew.Margin)
            rect.SetWidth(pcbnew.FromMM(pen))
            board.Add(rect)
        layers += ",Margin"

    with tempfile.TemporaryDirectory() as tmp:
        copy, pdf = Path(tmp) / "copy.kicad_pcb", Path(tmp) / "plot.pdf"
        pcbnew.SaveBoard(str(copy), board)
        # Scale 1 maps board mm 1:1 onto the page; --sp draws pad outlines and numbers on
        # the fab layers; --ev drops value text; the dark ground suits the default colors.
        ground = "#fdf6e3" if a.light else "#002b36"
        cmd = ["kicad-cli", "pcb", "export", "pdf", "--mode-single", "--layers", layers,
               "--scale", "1", "--sp", "--ev", "--bg-color", ground, "-o", str(pdf), str(copy)]
        if a.bottom:
            cmd.append("--mirror")
        subprocess.run(cmd, check=True, capture_output=True)
        info = subprocess.run(["pdfinfo", str(pdf)], check=True, capture_output=True, text=True).stdout
        page_w = float(next(l for l in info.splitlines() if l.startswith("Page size")).split()[2]) * 25.4 / 72
        # A mirrored plot flips the page left to right: page x = page width - board x.
        left = page_w - x1 if a.bottom else x0
        w, h = x1 - x0, y1 - y0
        dpi = max(150, min(6000, round(a.px * 25.4 / max(w, h))))
        px = lambda mm: str(round(mm * dpi / 25.4))
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", "-x", px(left), "-y", px(y0),
                        "-W", px(w), "-H", px(h), str(pdf), str(out.with_suffix(""))], check=True)
    print(f"{out}: {px(w)}x{px(h)} px, board box {x0:.1f},{y0:.1f} to {x1:.1f},{y1:.1f} mm, layers {layers}")


if __name__ == "__main__":
    main()
```

Examples: a regulator whose feedback track runs over a gap in the inner ground plane, and a USB pair on the back that runs over a supply pour on the second inner layer:

```bash
python3 pcb_shot.py board.kicad_pcb CLD-REVIEW-BATTERY-4-shots/P1-2.png \
    --layers In1.Cu,F.Cu,F.Fab --around U3,R13 --mark U3 --clear-fill F.Cu
python3 pcb_shot.py board.kicad_pcb CLD-REVIEW-BATTERY-4-shots/P2-1.png --bottom \
    --layers In2.Cu,B.Cu,B.Fab --area 154,125,163,141 --clear-fill B.Cu \
    --nets '/USB HUB/WIFI_USB_*' --fill-nets 5VA --mark-area 159.1,129.2,160.8,132.5
```

- Crop with `--around` the parts involved, or with `--area` (board millimeters) for a stretch of track, a plane gap or a board edge. Frame the problem with enough surroundings to see where its tracks go: the default 3 mm margin, 5 to 30 mm across. Split a long route into the stretches that matter.
- Mark the exact spot with `--mark` or `--mark-area`, repeated for each spot: the pin, via, gap or edge, not the whole crop. The box is drawn just outside the area given; make the area a little larger than the evidence so the line frames it, since a line over a 0.1 mm gap hides the gap.
- Find coordinates in the board tables or with `pcbnew`: `fp.GetPosition()`, `pad.GetBoundingBox()`, `track.GetStart()`; `pcbnew.ToMM()` converts. To find where a track runs over a pour, test points along it with `zone.GetFilledPolysList(layer).Contains(pcbnew.VECTOR2I_MM(x, y))`.

### Check every image

Open each PNG before linking it. Without its caption it must show the problem: the marked spot is visible, pad numbers and designators are legible, and nothing on a layer that does not bear on the finding draws the eye. Otherwise change the layers or the crop and plot again. If no plot shows the problem, leave the image out and say why in the finding's body.

The copy you are shown may be scaled down: crop the PNG around the mark and enlarge it to check detail under about 0.2 mm. When the picture disagrees with the finding, measure the board and correct the finding.
