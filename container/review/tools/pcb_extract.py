#!/usr/bin/env python3
"""Dump board facts from a KiCad board with the pcbnew module for review.

Writes into the output directory:
  pcb_summary.txt     layers, stackup, counts, track width histogram, zones
  pcb_footprints.txt  every footprint with every pad (net, geometry, drill)
"""
import sys, collections, math
import pcbnew

def mm(v):
    return pcbnew.ToMM(v)

def main(path, outdir):
    b = pcbnew.LoadBoard(path)
    ds = b.GetDesignSettings()
    out = []
    out.append(f"board: {path}")
    out.append(f"copper layers: {b.GetCopperLayerCount()}  thickness: {mm(ds.GetBoardThickness()):.4f} mm")
    bb = b.GetBoardEdgesBoundingBox()
    out.append(f"edge bbox: x {mm(bb.GetLeft()):.3f}..{mm(bb.GetRight()):.3f}  y {mm(bb.GetTop()):.3f}..{mm(bb.GetBottom()):.3f}  size {mm(bb.GetWidth()):.3f} x {mm(bb.GetHeight()):.3f}")
    try:
        st = ds.GetStackupDescriptor()
        out.append("stackup:")
        for it in st.GetList():
            try:
                out.append(f"  {it.GetLayerName() or it.GetTypeName():<14} type={it.GetTypeName():<12} thick={mm(it.GetThickness()):.4f} mat={it.GetMaterial()} er={it.GetEpsilonR():.3f} tand={it.GetLossTangent():.4f}")
            except Exception as e:
                out.append(f"  (item error {e})")
    except Exception as e:
        out.append(f"stackup error: {e}")
    out.append(f"min clearance {mm(ds.m_MinClearance):.4f}  min track {mm(ds.m_TrackMinWidth):.4f}  min via {mm(ds.m_ViasMinSize):.4f}  min hole {mm(ds.m_MinThroughDrill):.4f}  hole2hole {mm(ds.m_HoleToHoleMin):.4f}  edge clr {mm(ds.m_CopperEdgeClearance):.4f}  min annular {mm(ds.m_ViasMinAnnularWidth):.4f}")
    tracks = list(b.GetTracks())
    seg = collections.Counter(); via = collections.Counter(); arcs = 0
    for t in tracks:
        if t.GetClass() == "PCB_VIA":
            via[(round(mm(t.GetWidth(pcbnew.F_Cu)), 4), round(mm(t.GetDrillValue()), 4), t.GetViaType())] += 1
        else:
            if t.GetClass() == "PCB_ARC":
                arcs += 1
            seg[(b.GetLayerName(t.GetLayer()), round(mm(t.GetWidth()), 4))] += 1
    out.append(f"tracks: {sum(seg.values())} segments/arcs ({arcs} arcs), vias: {sum(via.values())}")
    out.append("vias by (pad, drill, type): " + ", ".join(f"{k}:{v}" for k, v in sorted(via.items())))
    out.append("track widths by layer:")
    for (layer, w), n in sorted(seg.items()):
        out.append(f"  {layer:<8} {w:>7.4f} mm x {n}")
    out.append("zones:")
    for z in b.Zones():
        bbz = z.GetBoundingBox()
        layers = ",".join(b.GetLayerName(l) for l in z.GetLayerSet().Seq())
        out.append(f"  net={z.GetNetname() or '-':<40} layers={layers:<28} prio={z.GetAssignedPriority()} keepout={z.GetIsRuleArea()} name='{z.GetZoneName()}' bbox=({mm(bbz.GetLeft()):.1f},{mm(bbz.GetTop()):.1f})-({mm(bbz.GetRight()):.1f},{mm(bbz.GetBottom()):.1f}) clr={mm(z.GetLocalClearance().value() if hasattr(z.GetLocalClearance(),'value') and z.GetLocalClearance().has_value() else 0):.3f} minw={mm(z.GetMinThickness()):.3f} conn={z.GetPadConnection()}")
    open(f"{outdir}/pcb_summary.txt", "w").write("\n".join(out) + "\n")

    fo = []
    for fp in sorted(b.GetFootprints(), key=lambda f: f.GetReference()):
        pos = fp.GetPosition()
        fo.append(f"## {fp.GetReference()} value={fp.GetValue()} fp={fp.GetFPID().GetUniStringLibId()} side={'B' if fp.IsFlipped() else 'F'} at=({mm(pos.x):.4f},{mm(pos.y):.4f}) rot={fp.GetOrientationDegrees():.1f} dnp={fp.IsDNP()} pads={fp.GetPadCount()}")
        ang = -math.radians(fp.GetOrientationDegrees())
        for p in fp.Pads():
            pp = p.GetPosition()
            dx, dy = mm(pp.x - pos.x), mm(pp.y - pos.y)
            # rotate back into the footprint's own frame (KiCad Y axis points down)
            rx = dx * math.cos(ang) - dy * math.sin(ang)
            ry = dx * math.sin(ang) + dy * math.cos(ang)
            if fp.IsFlipped():
                rx = -rx
            sz = p.GetSize(pcbnew.F_Cu) if not fp.IsFlipped() else p.GetSize(pcbnew.B_Cu)
            shape = p.ShowPadShape(pcbnew.F_Cu if not fp.IsFlipped() else pcbnew.B_Cu)
            drill = p.GetDrillSize()
            attr = p.ShowPadAttr()
            fo.append(f"   pad {p.GetNumber():>5} net={p.GetNetname() or '-':<44} {attr:<10} {shape:<10} size={mm(sz.x):.3f}x{mm(sz.y):.3f} drill={mm(drill.x):.3f} rel=({rx:.4f},{ry:.4f}) abs=({mm(pp.x):.4f},{mm(pp.y):.4f}) padrot={p.GetOrientationDegrees():.0f}")
    open(f"{outdir}/pcb_footprints.txt", "w").write("\n".join(fo) + "\n")
    print("ok", len(tracks), "track items,", len(list(b.GetFootprints())), "footprints")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
