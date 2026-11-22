#!/usr/bin/env python3
"""Report copper of other nets that overlaps or comes close to given pads/vias on a KiCad board."""
import sys, math
import pcbnew
mm = pcbnew.ToMM

def seg_point_dist(ax, ay, bx, by, px, py):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

def main(path, refs):
    b = pcbnew.LoadBoard(path)
    targets = []
    for fp in b.GetFootprints():
        if fp.GetReference() in refs:
            for p in fp.Pads():
                pos = p.GetPosition()
                sz = p.GetSize(pcbnew.F_Cu)
                targets.append((fp.GetReference() + "." + p.GetNumber(), p.GetNetname(), mm(pos.x), mm(pos.y),
                                mm(max(sz.x, sz.y)) / 2, mm(p.GetDrillSize().x) / 2, p.GetAttribute()))
    for name, net, x, y, r, dr, attr in targets:
        print(f"{name} net={net} at ({x:.4f},{y:.4f}) pad radius {r:.4f} drill radius {dr:.4f} attr={attr}")
        for t in b.GetTracks():
            if t.GetNetname() == net:
                continue
            layer = b.GetLayerName(t.GetLayer())
            if t.GetClass() == "PCB_VIA":
                p = t.GetPosition()
                d = math.hypot(mm(p.x) - x, mm(p.y) - y)
                w = mm(t.GetWidth(pcbnew.F_Cu)) / 2
                if d - w - r < 0.3:
                    print(f"    VIA net={t.GetNetname()} at ({mm(p.x):.4f},{mm(p.y):.4f}) centre dist {d:.4f} edge-to-pad {d - w - r:.4f} edge-to-hole {d - w - dr:.4f}")
            else:
                s, e = t.GetStart(), t.GetEnd()
                d = seg_point_dist(mm(s.x), mm(s.y), mm(e.x), mm(e.y), x, y)
                w = mm(t.GetWidth()) / 2
                if d - w - r < 0.3:
                    print(f"    TRACK {layer} net={t.GetNetname()} w={2*w:.3f} ({mm(s.x):.4f},{mm(s.y):.4f})-({mm(e.x):.4f},{mm(e.y):.4f}) centreline dist {d:.4f} edge-to-pad {d - w - r:.4f} edge-to-hole {d - w - dr:.4f}")

if __name__ == "__main__":
    main(sys.argv[1], set(sys.argv[2].split(",")))
