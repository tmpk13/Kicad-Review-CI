#!/usr/bin/env python3
"""Compare how the same IC is wired on two boards.

For every pin of the IC, print the other pins found on the same net on each board, reduced to
"value.pinname" so reference designators do not matter. Lines that differ are marked with '!'.
Usage: cmp_ic.py netlistA refA netlistB refB
"""
import sys, re, sexpdata

def sym(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x

def fa(n, name):
    return [c for c in n if isinstance(c, list) and c and sym(c[0]) == name]

def load(path):
    d = sexpdata.loads(open(path).read())
    comps, dnp = {}, set()
    for c in fa(fa(d, "components")[0], "comp"):
        ref = str(sym(fa(c, "ref")[0][1]))
        comps[ref] = str(sym(fa(c, "value")[0][1]))
        for p in fa(c, "property"):
            if str(sym(fa(p, "name")[0][1])) == "dnp":
                dnp.add(ref)
    nets = {}
    pins = {}
    for n in fa(fa(d, "nets")[0], "net"):
        name = str(sym(fa(n, "name")[0][1]))
        nodes = []
        for nd in fa(n, "node"):
            ref = str(sym(fa(nd, "ref")[0][1])); pin = str(sym(fa(nd, "pin")[0][1]))
            pf = fa(nd, "pinfunction"); pf = str(sym(pf[0][1])) if pf else ""
            nodes.append((ref, pin, pf))
            pins.setdefault(ref, {})[pin] = name
        nets[name] = nodes
    return comps, nets, pins, dnp

def sig(comps, nets, pins, dnp, ref, pin):
    net = pins[ref][pin]
    if net in ("GND",):
        return "GND"
    out = []
    for r, p, pf in nets[net]:
        if r == ref:
            continue
        if r.startswith("TP") or r in dnp:
            continue
        pf = re.sub(r"_\w+$", "", pf)
        v = comps.get(r, "?")
        if re.match(r"^(R|C|L|FB|D)\d", r):
            out.append(v)
        else:
            out.append(f"{v}.{pf or p}")
    label = net if not net.startswith("Net-(") and not net.startswith("unconnected") else ""
    label = label.split("/")[-1]
    return (label + " | " if label else "") + ", ".join(sorted(out)) if out or label else "(open)"

a, refa, b, refb = sys.argv[1:5]
A, B = load(a), load(b)
pa, pb = A[2][refa], B[2][refb]
def key(p):
    m = re.match(r"(\d+)", p); return (int(m.group(1)) if m else 999, p)
for p in sorted(set(pa) | set(pb), key=key):
    sa = sig(*A, refa, p) if p in pa else "-"
    sb = sig(*B, refb, p) if p in pb else "-"
    print(("  " if sa == sb else "! ") + f"{p:>4}  A: {sa}\n" + ("" if sa == sb else f"        B: {sb}\n"), end="")
