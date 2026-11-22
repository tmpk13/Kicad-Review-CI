# Review tools

Run with `python3` (the `pcbnew` module and `sexpdata` are installed).

- `parse_netlist.py NETLIST OUTDIR`: turn a `kicadsexpr` netlist into `components.tsv`
  (every field), `nets.txt` (net, then REF.PIN[function|type] nodes) and `pins.txt`
  (each part's pins with their nets). The job runs it on every project.
- `pcb_extract.py BOARD OUTDIR`: `pcb_summary.txt` (layers, stackup, design rules, track
  widths by layer, vias, zones) and `pcb_footprints.txt` (every pad: net, shape, size,
  drill, position). The job runs it on every board.
- `cmp_ic.py NETLIST_A REF_A NETLIST_B REF_B`: compare how one IC is wired on two boards
  (or two revisions), pin by pin, by the value and pin name of what each pin meets.
  Lines that differ start with `!`. Export the other netlist with
  `kicad-cli sch export netlist --format kicadsexpr` from a `git worktree` of the other
  revision.
- `shorts.py BOARD REF [REF ...]`: list copper of other nets that overlaps or comes
  close to the pads of the given parts.
- `zoom.sh PDF OUT.png X0 Y0 W H [DPI]`: crop a region (in mm) of a 1:1 board plot into a
  PNG you can look at. Plot layers with
  `kicad-cli pcb export pdf --mode-single --layers F.Cu,Edge.Cuts --scale 1 -o plot.pdf BOARD`.
- `brief_lint.py REPORT.md`: flag finding briefs that name a designator, part number,
  pin or net, that are bold text instead of a `####` heading, or that run past about
  25 words; the Function, Verdict and First action lines get the same name check.
  Values ("100 ohm") pass. Run it before finishing; the run record shows its result.
