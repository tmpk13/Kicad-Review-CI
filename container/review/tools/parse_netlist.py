#!/usr/bin/env python3
"""Parse a KiCad kicadsexpr netlist into flat text tables for review.

Outputs (in the given output directory):
  components.tsv  one row per component with all fields
  nets.txt        one line per net: name, then REF.PIN[pinfunction|pintype] nodes
  pins.txt        one block per component: pin number, pin function, pin type, net
"""
import sys, collections
import sexpdata

def sym(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x

def find_all(node, name):
    return [c for c in node if isinstance(c, list) and c and sym(c[0]) == name]

def find(node, name):
    r = find_all(node, name)
    return r[0] if r else None

def val(node, name, default=""):
    f = find(node, name)
    if f is None or len(f) < 2:
        return default
    return str(sym(f[1]))

def main(path, outdir):
    data = sexpdata.loads(open(path).read())
    comps = {}
    for c in find_all(find(data, "components"), "comp"):
        ref = val(c, "ref")
        fields = {}
        fl = find(c, "fields")
        if fl:
            for f in find_all(fl, "field"):
                name = val(f, "name")
                value = str(sym(f[2])) if len(f) > 2 else ""
                fields[name] = value
        props = {}
        for p in find_all(c, "property"):
            props[val(p, "name")] = val(p, "value")
        sp = find(c, "sheetpath")
        ls = find(c, "libsource")
        comps[ref] = dict(
            ref=ref, value=val(c, "value"), footprint=val(c, "footprint"),
            datasheet=val(c, "datasheet"), description=val(c, "description"),
            sheet=val(sp, "names") if sp else "", lib=val(ls, "lib") if ls else "",
            part=val(ls, "part") if ls else "", fields=fields, props=props)
    nets = collections.OrderedDict()
    pins = collections.defaultdict(list)
    for n in find_all(find(data, "nets"), "net"):
        name = val(n, "name")
        nodes = []
        for nd in find_all(n, "node"):
            ref, pin = val(nd, "ref"), val(nd, "pin")
            pf, pt = val(nd, "pinfunction"), val(nd, "pintype")
            nodes.append((ref, pin, pf, pt))
            pins[ref].append((pin, pf, pt, name))
        nets[name] = nodes
    allfields = sorted({k for c in comps.values() for k in c["fields"]})
    allprops = sorted({k for c in comps.values() for k in c["props"]})
    def refkey(r):
        import re
        m = re.match(r"([A-Za-z_#]+)(\d+)(.*)", r)
        return (m.group(1), int(m.group(2)), m.group(3)) if m else (r, 0, "")
    with open(f"{outdir}/components.tsv", "w") as f:
        f.write("\t".join(["ref", "value", "footprint", "sheet", "lib", "part", "description", "datasheet"] + allfields + ["prop:" + p for p in allprops]) + "\n")
        for ref in sorted(comps, key=refkey):
            c = comps[ref]
            f.write("\t".join([c["ref"], c["value"], c["footprint"], c["sheet"], c["lib"], c["part"], c["description"], c["datasheet"]] + [c["fields"].get(k, "") for k in allfields] + [c["props"].get(k, "") for k in allprops]) + "\n")
    with open(f"{outdir}/nets.txt", "w") as f:
        for name, nodes in nets.items():
            f.write(name + " :: " + "  ".join(f"{r}.{p}[{pf}|{pt}]" for r, p, pf, pt in sorted(nodes, key=lambda x: (refkey(x[0]), x[1]))) + "\n")
    def pinkey(p):
        import re
        m = re.match(r"([A-Za-z]*)(\d*)", p[0])
        return (m.group(1), int(m.group(2)) if m.group(2) else 0)
    with open(f"{outdir}/pins.txt", "w") as f:
        for ref in sorted(comps, key=refkey):
            c = comps[ref]
            f.write(f"## {ref}  value={c['value']}  fp={c['footprint']}  sheet={c['sheet']}  mpn={c['fields'].get('MPN', c['fields'].get('Mfr. Part #', ''))}\n")
            for pin, pf, pt, net in sorted(pins.get(ref, []), key=pinkey):
                f.write(f"   {pin:>5}  {pf:<28} {pt:<14} {net}\n")
    print(f"{len(comps)} components, {len(nets)} nets; fields: {allfields}; props: {allprops}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
