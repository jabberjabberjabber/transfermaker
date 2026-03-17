#!/usr/bin/env python3

import argparse
import math
import re
import sys

from lxml import etree
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.validation import make_valid

# ── Constants ─────────────────────────────────────────────────────────────────

SVGNS       = "http://www.w3.org/2000/svg"
CURVE_STEPS = 32    # line segments per bezier/arc when flattening to polygon

UNIT_TO_MM = {
    "mm": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "px": 25.4 / 96.0,
    "pt": 25.4 / 72.0,
    "pc": 25.4 / 6.0,
}

# ── Unit helpers ──────────────────────────────────────────────────────────────

def parse_dimension(s):
    m = re.match(r"([\d.]+)\s*(mm|cm|in|px|pt|pc)?", (s or "").strip())
    return (float(m.group(1)), m.group(2) or "px") if m else (None, None)

def get_mm_per_unit(root):
    """Return how many mm correspond to one SVG user unit."""
    w_val, w_unit = parse_dimension(root.get("width", ""))
    vb = root.get("viewBox", "")

    if w_val and w_unit in UNIT_TO_MM:
        w_mm = w_val * UNIT_TO_MM[w_unit]
        if vb:
            parts = vb.split()
            if len(parts) == 4 and float(parts[2]) > 0:
                return w_mm / float(parts[2])
        return UNIT_TO_MM[w_unit]

    return 25.4 / 96.0   # fallback: CSS pixels at 96 dpi

# ── SVG path → flat polygon points ────────────────────────────────────────────

def _tokenize(d):
    return re.findall(
        r"[MmZzLlHhVvCcSsQqTtAa]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?",
        d
    )

def _cubic(x0, y0, x1, y1, x2, y2, x3, y3, n):
    pts = []
    for i in range(1, n + 1):
        t = i / n; mt = 1 - t
        pts.append((
            mt**3*x0 + 3*mt**2*t*x1 + 3*mt*t**2*x2 + t**3*x3,
            mt**3*y0 + 3*mt**2*t*y1 + 3*mt*t**2*y2 + t**3*y3,
        ))
    return pts

def _quadratic(x0, y0, x1, y1, x2, y2, n):
    pts = []
    for i in range(1, n + 1):
        t = i / n; mt = 1 - t
        pts.append((mt**2*x0 + 2*mt*t*x1 + t**2*x2,
                    mt**2*y0 + 2*mt*t*y1 + t**2*y2))
    return pts

def _arc(x1, y1, rx, ry, phi_deg, large_arc, sweep, x2, y2, n):
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    phi = math.radians(phi_deg)
    cp, sp = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x1 - x2) / 2, (y1 - y2) / 2
    x1p =  cp*dx2 + sp*dy2
    y1p = -sp*dx2 + cp*dy2
    rx, ry = abs(rx), abs(ry)
    lam = (x1p/rx)**2 + (y1p/ry)**2
    if lam > 1:
        s = math.sqrt(lam); rx *= s; ry *= s
    rx2, ry2 = rx**2, ry**2
    num = max(0.0, rx2*ry2 - rx2*y1p**2 - ry2*x1p**2)
    den = rx2*y1p**2 + ry2*x1p**2
    sq = (math.sqrt(num / den) if den else 0.0) * (-1 if large_arc == sweep else 1)
    cxp =  sq*rx*y1p/ry
    cyp = -sq*ry*x1p/rx
    cx = cp*cxp - sp*cyp + (x1 + x2) / 2
    cy = sp*cxp + cp*cyp + (y1 + y2) / 2

    def angle(ux, uy, vx, vy):
        mag = math.sqrt(ux**2 + uy**2) * math.sqrt(vx**2 + vy**2)
        a = math.acos(max(-1.0, min(1.0, (ux*vx + uy*vy) / mag))) if mag else 0.0
        return -a if ux*vy - uy*vx < 0 else a

    t1 = angle(1, 0, (x1p - cxp)/rx, (y1p - cyp)/ry)
    dt = angle((x1p - cxp)/rx, (y1p - cyp)/ry, (-x1p - cxp)/rx, (-y1p - cyp)/ry)
    if not sweep and dt > 0: dt -= 2 * math.pi
    if     sweep and dt < 0: dt += 2 * math.pi

    pts = []
    for i in range(1, n + 1):
        th = t1 + (i / n) * dt
        pts.append((cp*rx*math.cos(th) - sp*ry*math.sin(th) + cx,
                    sp*rx*math.cos(th) + cp*ry*math.sin(th) + cy))
    return pts

def flatten_path(d):
    """
    Parse an SVG path d string and return a list of subpaths.
    Each subpath is a list of (x, y) float tuples.
    Bezier curves and arcs are sampled to line segments.
    """
    tokens = _tokenize(d)
    pos = 0

    def consume(n):
        nonlocal pos
        vals = [float(tokens[pos + i]) for i in range(n)]
        pos += n
        return vals

    subpaths = []
    current  = []
    cx = cy = sx = sy = 0.0
    last_ctrl = None
    cmd = None
    S = CURVE_STEPS

    while pos < len(tokens):
        tok = tokens[pos]

        if re.match(r"[A-Za-z]", tok):
            cmd = tok; pos += 1
            if cmd in ('Z', 'z'):
                if current:
                    current.append(current[0])  # close ring
                    subpaths.append(current)
                    current = []
                cx, cy = sx, sy
                cmd = None
            continue

        if cmd is None:
            pos += 1; continue

        if cmd in ('M', 'm'):
            if current: subpaths.append(current)
            x, y = consume(2)
            if cmd == 'm': x += cx; y += cy
            cx, cy = x, y; sx, sy = cx, cy
            current = [(cx, cy)]
            cmd = 'L' if cmd == 'M' else 'l'

        elif cmd in ('L', 'l'):
            x, y = consume(2)
            if cmd == 'l': x += cx; y += cy
            current.append((x, y)); cx, cy = x, y

        elif cmd in ('H', 'h'):
            x, = consume(1)
            if cmd == 'h': x += cx
            current.append((x, cy)); cx = x

        elif cmd in ('V', 'v'):
            y, = consume(1)
            if cmd == 'v': y += cy
            current.append((cx, y)); cy = y

        elif cmd in ('C', 'c'):
            x1, y1, x2, y2, x3, y3 = consume(6)
            if cmd == 'c': x1+=cx; y1+=cy; x2+=cx; y2+=cy; x3+=cx; y3+=cy
            current.extend(_cubic(cx, cy, x1, y1, x2, y2, x3, y3, S))
            last_ctrl = (x2, y2); cx, cy = x3, y3

        elif cmd in ('S', 's'):
            x2, y2, x3, y3 = consume(4)
            if cmd == 's': x2+=cx; y2+=cy; x3+=cx; y3+=cy
            x1 = 2*cx - last_ctrl[0] if last_ctrl else cx
            y1 = 2*cy - last_ctrl[1] if last_ctrl else cy
            current.extend(_cubic(cx, cy, x1, y1, x2, y2, x3, y3, S))
            last_ctrl = (x2, y2); cx, cy = x3, y3

        elif cmd in ('Q', 'q'):
            x1, y1, x2, y2 = consume(4)
            if cmd == 'q': x1+=cx; y1+=cy; x2+=cx; y2+=cy
            current.extend(_quadratic(cx, cy, x1, y1, x2, y2, S))
            last_ctrl = (x1, y1); cx, cy = x2, y2

        elif cmd in ('T', 't'):
            x2, y2 = consume(2)
            if cmd == 't': x2+=cx; y2+=cy
            x1 = 2*cx - last_ctrl[0] if last_ctrl else cx
            y1 = 2*cy - last_ctrl[1] if last_ctrl else cy
            current.extend(_quadratic(cx, cy, x1, y1, x2, y2, S))
            last_ctrl = (x1, y1); cx, cy = x2, y2

        elif cmd in ('A', 'a'):
            rx, ry, rot, laf, sf, x2, y2 = consume(7)
            if cmd == 'a': x2 += cx; y2 += cy
            current.extend(_arc(cx, cy, rx, ry, rot, int(laf), int(sf), x2, y2, S))
            cx, cy = x2, y2; last_ctrl = None

        else:
            pos += 1

    if current:
        subpaths.append(current)

    return subpaths

# ── Shapely helpers ───────────────────────────────────────────────────────────

def subpaths_to_shapely(subpaths):
    """
    Convert a list of subpaths to a Shapely geometry using even-odd fill.
    Each subpath is XOR'd into the accumulated result, so nested subpaths
    naturally become holes.
    """
    result = None
    for sp in subpaths:
        if len(sp) < 3:
            continue
        try:
            p = Polygon(sp)
            if not p.is_valid:
                p = make_valid(p)
            if p.is_empty:
                continue
            result = p if result is None else result.symmetric_difference(p)
        except Exception:
            continue

    if result is not None and not result.is_valid:
        result = make_valid(result)

    return result if (result is not None and not result.is_empty) else None

def iter_polygons(geom):
    """Yield individual Polygon objects from any Shapely geometry."""
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == 'Polygon':
        yield geom
    elif hasattr(geom, 'geoms'):
        for g in geom.geoms:
            yield from iter_polygons(g)

def remove_artifacts(geom, r):
    """
    Remove numerical artifacts (tiny dots and thin slivers) left by buffer ops.

    Two passes:
      1. A tiny secondary morphological opening (0.5 % of r) collapses slivers
         that survived the main opening due to floating-point rounding.
      2. An area filter drops any remaining sub-feature polygons whose area is
         less than 5 % of the smallest theoretically cuttable circle (π r²).
    """
    if geom is None or geom.is_empty:
        return geom

    eps = max(r * 0.005, 0.01)
    try:
        cleaned = geom.buffer(-eps, resolution=4).buffer(eps, resolution=4)
        if not cleaned.is_valid:
            cleaned = make_valid(cleaned)
        if cleaned.is_empty:
            cleaned = geom   # don't over-clean
    except Exception:
        cleaned = geom

    min_area = math.pi * r * r * 0.05
    polys = [p for p in iter_polygons(cleaned) if p.area >= min_area]
    if not polys:
        return cleaned   # fallback: return what we had rather than nothing
    return polys[0] if len(polys) == 1 else unary_union(polys)

def ring_to_d(coords):
    """Convert a coordinate sequence to an SVG path fragment."""
    pts = list(coords)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]   # drop the repeated closing point
    if len(pts) < 3:
        return None
    d = f"M {pts[0][0]:.4f},{pts[0][1]:.4f}"
    for x, y in pts[1:]:
        d += f" L {x:.4f},{y:.4f}"
    return d + " Z"

def polygon_to_d(poly, simplify_tol=0.0):
    """Convert a Shapely Polygon (with optional holes) to an SVG path d string."""
    if simplify_tol > 0:
        poly = poly.simplify(simplify_tol, preserve_topology=True)
    if poly.is_empty:
        return None

    parts = []
    ext = ring_to_d(poly.exterior.coords)
    if not ext:
        return None
    parts.append(ext)

    for interior in poly.interiors:
        inner = ring_to_d(interior.coords)
        if inner:
            parts.append(inner)

    return " ".join(parts)

def geometry_to_path_strings(geom, simplify_tol=0.0):
    """Return one SVG path d string per Polygon component in geom."""
    return [d for poly in iter_polygons(geom)
            if (d := polygon_to_d(poly, simplify_tol))]

# ── Fill detection ────────────────────────────────────────────────────────────

def is_filled(elem):
    """Return True if this element has a visible fill."""
    style_fill = None
    for decl in elem.get("style", "").split(";"):
        decl = decl.strip()
        if decl.lower().startswith("fill:"):
            style_fill = decl[5:].strip().lower()
    effective = style_fill if style_fill is not None else elem.get("fill", "").strip().lower()
    return effective != "none"

# ── Main ──────────────────────────────────────────────────────────────────────

def process_svg(input_path, output_path, min_size_mm, simplify_tol_mm=0.0, keep_thin=False, thicken_thin=False):
    tree = etree.parse(input_path)
    root = tree.getroot()

    mm_per_unit = get_mm_per_unit(root)
    r           = (min_size_mm / 2.0) / mm_per_unit   # buffer radius in user units
    simplify    = simplify_tol_mm / mm_per_unit if simplify_tol_mm > 0 else 0.0

    print(f"Document:  {mm_per_unit:.6f} mm/unit")
    print(f"Min size:  {min_size_mm} mm  →  buffer radius {r:.4f} user units")
    if simplify:
        print(f"Simplify:  {simplify_tol_mm} mm  →  {simplify:.4f} user units")
    print()

    path_elems = (
        root.findall(f".//{{{SVGNS}}}path") or
        root.findall(".//path")
    )

    replacements = []

    for elem in path_elems:
        if not is_filled(elem):
            continue

        d = elem.get("d", "").strip()
        if not d:
            continue

        try:
            subpaths = flatten_path(d)
        except Exception as e:
            print(f"  Warning: could not parse '{elem.get('id', '?')}': {e}")
            continue

        geom = subpaths_to_shapely(subpaths)
        if geom is None:
            continue

        # 1. Morphological closing: buffer(+r).buffer(-r)
        #    Fills any gap narrower than 2r between nearby same-colour shapes.
        #    This fixes the common "two outlines with a thin gap" tracing
        #    artifact where a raster stroke is vectorised as two filled hulls.
        # 2. Morphological opening: buffer(-r).buffer(+r)
        #    Removes protrusions/features narrower than 2r from the result.
        try:
            closed = geom.buffer(r, resolution=16).buffer(-r, resolution=16)
            if not closed.is_valid:
                closed = make_valid(closed)
            thick = closed.buffer(-r, resolution=16).buffer(r, resolution=16)
            if not thick.is_valid:
                thick = make_valid(thick)
            thick = remove_artifacts(thick, r)
            thin = geom.difference(thick)
            if not thin.is_valid:
                thin = make_valid(thin)
            thin = remove_artifacts(thin, r)
        except Exception as e:
            print(f"  Warning: buffer failed for '{elem.get('id', '?')}': {e}")
            continue

        thin_ds  = geometry_to_path_strings(thin,  simplify)
        thick_ds = geometry_to_path_strings(thick, simplify)

        if not thin_ds and len(thick_ds) <= 1:
            continue   # nothing changed

        parent = elem.getparent()
        if parent is None:
            continue

        base_attrs = dict(elem.attrib)
        base_id    = base_attrs.pop("id", None)
        tag        = elem.tag
        new_elems  = []

        for i, pd in enumerate(thick_ds):
            ne = etree.Element(tag)
            ne.attrib.update(base_attrs)
            ne.set("d", pd)
            if base_id:
                ne.set("id", base_id if i == 0 else f"{base_id}_thick{i}")
            new_elems.append(ne)

        if thicken_thin or keep_thin:
            if thicken_thin:
                # Resolve fill color so the stroke matches the shape
                fill_color = None
                for decl in elem.get("style", "").split(";"):
                    decl = decl.strip()
                    if decl.lower().startswith("fill:"):
                        fill_color = decl[5:].strip()
                if not fill_color:
                    fill_color = elem.get("fill", "") or "currentColor"
                stroke_w = f"{min_size_mm / mm_per_unit:.4f}"

            for i, pd in enumerate(thin_ds):
                ne = etree.Element(tag)
                ne.attrib.update(base_attrs)
                ne.set("d", pd)
                if base_id:
                    ne.set("id", f"{base_id}_thin{i}")
                if thicken_thin:
                    ne.set("stroke", fill_color)
                    ne.set("stroke-width", stroke_w)
                    ne.set("stroke-linejoin", "round")
                    ne.set("stroke-linecap", "round")
                else:
                    ne.set("data-thin-region", "true")
                new_elems.append(ne)

        replacements.append((elem, parent, new_elems))
        msg = f"  '{base_id or '(no id)'}': {len(thick_ds)} thick part(s)"
        if thin_ds:
            if thicken_thin:
                msg += f", {len(thin_ds)} thin part(s) stroked at {min_size_mm}mm"
            elif keep_thin:
                msg += f", {len(thin_ds)} thin part(s) kept"
            else:
                msg += f", {len(thin_ds)} thin part(s) removed"
        print(msg)

    if not replacements:
        print("No thin regions found — output is a copy of input.")
    else:
        for elem, parent, new_elems in replacements:
            idx = list(parent).index(elem)
            parent.remove(elem)
            for k, ne in enumerate(new_elems):
                parent.insert(idx + k, ne)

    tree.write(output_path, pretty_print=True, xml_declaration=True, encoding="UTF-8")
    print(f"\nOutput written to: {output_path}")

def main():
    p = argparse.ArgumentParser(
        description="Remove thin/narrow regions from an SVG to make it ready for HTV cutting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Regions narrower than --min-size mm (thin sections, fine protrusions, comb-like
details, stray hairs, etc.) are removed so the output is immediately cuttable
and weedable on a vinyl cutter.  Each surviving thick region becomes its own
path element.

Use --thicken-thin to keep thin regions in the output with a stroke-width equal
to --min-size so they appear at the minimum cuttable width.  Avoids geometric
reconstruction artifacts while preserving the full silhouette.

Use --keep-thin to retain removed regions as separate paths tagged
data-thin-region="true" (useful for reviewing what was discarded).

The --simplify flag reduces output node count by merging near-collinear segments.
Useful for traced images with thousands of tiny segments. A value of 0.1 is a
good starting point for vinyl cutting; increase if output files are still too large.

Example:
  uv run main.py art.svg art_cut.svg --min-size 3.0 --simplify 0.1

Notes:
  SVG transforms are NOT applied. Flatten transforms in your editor first.
  Output paths are dense polylines, not bezier curves. Fine for vinyl cutting.
"""
    )
    p.add_argument("input",  help="Input SVG file")
    p.add_argument("output", help="Output SVG file")
    p.add_argument("--min-size",  type=float, default=3.0,
                   help="Minimum feature thickness in mm (default: 3.0)")
    p.add_argument("--simplify",  type=float, default=0.0,
                   help="Output simplification tolerance in mm (default: 0 = disabled). "
                        "Try 0.1 to reduce node count on traced images.")
    p.add_argument("--keep-thin", action="store_true",
                   help="Keep thin regions in output (tagged data-thin-region=true) "
                        "instead of discarding them.")
    p.add_argument("--thicken-thin", action="store_true",
                   help="Keep thin regions with stroke-width set to --min-size so they "
                        "render at the minimum cuttable width instead of being discarded.")
    args = p.parse_args()

    process_svg(args.input, args.output, args.min_size, args.simplify,
                args.keep_thin, args.thicken_thin)

if __name__ == "__main__":
    main()
