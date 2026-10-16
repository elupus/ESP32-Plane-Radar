#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["shapely>=2.1"]
# ///
"""Build terrain dataset (coast, lakes, borders, rivers + land/lake fill) for Sweden.

Source: Natural Earth 1:10m (public domain), plus its denser Europe-only lake
and river supplements. Lines are clipped to a box around Sweden, simplified,
quantized to uint16 within that box and split into short chunks with bounding
boxes so the renderer can cull everything off-screen cheaply. Land and lake
polygons are pre-triangulated here so the device only has to fillTriangle().

  uv run scripts/build_terrain_outlines.py [--cache DIR]
"""

from __future__ import annotations

import argparse
import json
import math
import urllib.request
from pathlib import Path

import shapely
from shapely import affinity
from shapely.geometry import box, shape

ROOT = Path(__file__).resolve().parents[1]
OUT_H = ROOT / "include" / "data" / "terrain_outlines.h"
OUT_CPP = ROOT / "src" / "data" / "terrain_outlines_data.cpp"

NE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/{name}.geojson"
)

# Sweden plus a margin wider than the largest radar range.
LAT_MIN, LAT_MAX = 54.5, 69.8
LON_MIN, LON_MAX = 10.0, 25.0

# Douglas-Peucker tolerance. The tightest range shows ~60 m per pixel.
SIMPLIFY_M = 40.0
# Points per chunk: smaller = finer culling, more bbox overhead.
CHUNK_POINTS = 32

# Layer ids, in draw order (later layers draw on top).
LAYER_RIVER = 0
LAYER_LAKE = 1
LAYER_COAST = 2
LAYER_BORDER = 3

# Fill layers (triangles), in draw order: land over the water disc, then
# lakes back over the land.
FILL_LAND = 0
FILL_LAKE = 1

FILL_SOURCES = [
    ("ne_10m_land", FILL_LAND),
    ("ne_10m_lakes", FILL_LAKE),
    ("ne_10m_lakes_europe", FILL_LAKE),
]
# Triangles per fill chunk.
CHUNK_TRIANGLES = 32
# Sort cell size (quantized units) used to keep chunks spatially compact.
SORT_CELL_Q = 2048

SOURCES = [
    ("ne_10m_rivers_lake_centerlines", LAYER_RIVER),
    ("ne_10m_rivers_europe", LAYER_RIVER),
    ("ne_10m_lakes", LAYER_LAKE),
    ("ne_10m_lakes_europe", LAYER_LAKE),
    ("ne_10m_coastline", LAYER_COAST),
    ("ne_10m_admin_0_boundary_lines_land", LAYER_BORDER),
]

Q = 65535
KM_PER_DEG = 111.0
COS_MID = math.cos(math.radians((LAT_MIN + LAT_MAX) / 2))


def load_geojson(name: str, cache: Path | None) -> dict:
    if cache is not None:
        path = cache / f"{name}.geojson"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    with urllib.request.urlopen(NE_URL.format(name=name), timeout=120) as resp:
        raw = resp.read()
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"{name}.geojson").write_bytes(raw)
    return json.loads(raw.decode("utf-8"))


def geometry_lines(geom: dict | None) -> list[list[tuple[float, float]]]:
    """Every line string / polygon ring as a list of (lon, lat)."""
    if not geom:
        return []
    t = geom["type"]
    c = geom["coordinates"]
    if t == "LineString":
        return [c]
    if t in ("MultiLineString", "Polygon"):
        return list(c)
    if t == "MultiPolygon":
        return [ring for poly in c for ring in poly]
    if t == "GeometryCollection":
        return [l for g in geom["geometries"] for l in geometry_lines(g)]
    return []


def inside(p: tuple[float, float]) -> bool:
    return LON_MIN <= p[0] <= LON_MAX and LAT_MIN <= p[1] <= LAT_MAX


def clip_to_region(line: list) -> list[list[tuple[float, float]]]:
    """Runs of the line inside the region, keeping one point either side so
    lines reach the edge (those get clamped during quantization)."""
    runs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    pts = [(float(p[0]), float(p[1])) for p in line]
    for i, p in enumerate(pts):
        keep = inside(p) or (i > 0 and inside(pts[i - 1])) or (
            i + 1 < len(pts) and inside(pts[i + 1]))
        if keep:
            cur.append(p)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) >= 2]


def to_km(p: tuple[float, float]) -> tuple[float, float]:
    return (p[0] * KM_PER_DEG * COS_MID, p[1] * KM_PER_DEG)


def simplify(pts: list[tuple[float, float]], tol_km: float) -> list[tuple[float, float]]:
    """Iterative Douglas-Peucker in locally-flat km."""
    if len(pts) < 3:
        return pts
    km = [to_km(p) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = km[a]
        bx, by = km[b]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        best, best_i = -1.0, -1
        for i in range(a + 1, b):
            px, py = km[i]
            if seg2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > best:
                best, best_i = d, i
        if best > tol_km:
            keep[best_i] = True
            stack.append((a, best_i))
            stack.append((best_i, b))
    return [p for p, k in zip(pts, keep) if k]


def quantize(p: tuple[float, float]) -> tuple[int, int]:
    qx = round((p[0] - LON_MIN) / (LON_MAX - LON_MIN) * Q)
    qy = round((p[1] - LAT_MIN) / (LAT_MAX - LAT_MIN) * Q)
    return (min(Q, max(0, qx)), min(Q, max(0, qy)))


def build_dataset(cache: Path | None):
    points: list[tuple[int, int]] = []
    chunks: list[tuple[int, int, int, int, int, int, int]] = []
    per_layer: dict[int, int] = {}

    lines_by_layer: list[tuple[int, list[tuple[int, int]]]] = []
    for name, layer in SOURCES:
        data = load_geojson(name, cache)
        for feat in data["features"]:
            for line in geometry_lines(feat.get("geometry")):
                for run in clip_to_region(line):
                    simp = simplify(run, SIMPLIFY_M / 1000.0)
                    q = [quantize(p) for p in simp]
                    q = [p for i, p in enumerate(q) if i == 0 or p != q[i - 1]]
                    if len(q) >= 2:
                        lines_by_layer.append((layer, q))

    lines_by_layer.sort(key=lambda item: item[0])
    for layer, q in lines_by_layer:
        # Chunks share their boundary point so the line stays continuous.
        start = 0
        while start < len(q) - 1:
            part = q[start:start + CHUNK_POINTS]
            first = len(points)
            points.extend(part)
            xs = [p[0] for p in part]
            ys = [p[1] for p in part]
            chunks.append((first, len(part), layer, min(xs), min(ys), max(xs), max(ys)))
            per_layer[layer] = per_layer.get(layer, 0) + len(part)
            start += CHUNK_POINTS - 1
    return points, chunks, per_layer


def polygons(geom) -> list:
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        return [p for g in geom.geoms for p in polygons(g)]
    return []


def build_fill(cache: Path | None):
    """Clip, simplify and triangulate the fill polygons."""
    region = box(LON_MIN, LAT_MIN, LON_MAX, LAT_MAX)
    kx = KM_PER_DEG * COS_MID
    tris: list[tuple[int, tuple[int, int], tuple[int, int], tuple[int, int]]] = []
    for name, layer in FILL_SOURCES:
        data = load_geojson(name, cache)
        for feat in data["features"]:
            geom = feat.get("geometry")
            if not geom:
                continue
            clipped = shape(geom).buffer(0).intersection(region)
            for poly in polygons(clipped):
                # Simplify in locally-flat km so the tolerance is isotropic.
                km = affinity.scale(poly, xfact=kx, yfact=KM_PER_DEG, origin=(0, 0))
                km = km.simplify(SIMPLIFY_M / 1000.0, preserve_topology=True)
                deg = affinity.scale(km, xfact=1 / kx, yfact=1 / KM_PER_DEG, origin=(0, 0))
                for part in polygons(deg):
                    for tri in polygons(shapely.constrained_delaunay_triangles(part)):
                        coords = list(tri.exterior.coords)[:3]
                        q = [quantize(c) for c in coords]
                        if len(set(q)) == 3:
                            tris.append((layer, q[0], q[1], q[2]))

    def sort_key(t):
        cx = (t[1][0] + t[2][0] + t[3][0]) // 3 // SORT_CELL_Q
        cy = (t[1][1] + t[2][1] + t[3][1]) // 3 // SORT_CELL_Q
        return (t[0], cy, cx if cy % 2 == 0 else -cx)

    tris.sort(key=sort_key)
    chunks = []
    per_layer: dict[int, int] = {}
    i = 0
    while i < len(tris):
        layer = tris[i][0]
        j = i
        while j < len(tris) and j - i < CHUNK_TRIANGLES and tris[j][0] == layer:
            j += 1
        xs = [p[0] for t in tris[i:j] for p in t[1:]]
        ys = [p[1] for t in tris[i:j] for p in t[1:]]
        chunks.append((i, j - i, layer, min(xs), min(ys), max(xs), max(ys)))
        per_layer[layer] = per_layer.get(layer, 0) + (j - i)
        i = j
    return [t[1:] for t in tris], chunks, per_layer


def render_header(point_count: int, chunk_count: int, tri_count: int,
                  fill_chunk_count: int) -> str:
    return "\n".join([
        "// Generated by scripts/build_terrain_outlines.py — do not edit.",
        "#pragma once",
        "",
        "#include <cstddef>",
        "#include <cstdint>",
        "",
        "namespace data::terrain {",
        "",
        "enum Layer : uint8_t {",
        f"  kLayerRiver = {LAYER_RIVER},",
        f"  kLayerLake = {LAYER_LAKE},",
        f"  kLayerCoast = {LAYER_COAST},",
        f"  kLayerBorder = {LAYER_BORDER},",
        "};",
        "",
        "enum FillLayer : uint8_t {",
        f"  kFillLand = {FILL_LAND},",
        f"  kFillLake = {FILL_LAKE},",
        "};",
        "",
        "/** Quantized position: lon/lat = min + q / 65535 * span. */",
        "struct Point {",
        "  uint16_t x;  // longitude",
        "  uint16_t y;  // latitude",
        "};",
        "",
        "struct Triangle {",
        "  Point a;",
        "  Point b;",
        "  Point c;",
        "};",
        "",
        "/**",
        " * A run of consecutive points forming one polyline (kChunks), or of",
        " * consecutive triangles (kFillChunks), with its bbox.",
        " */",
        "struct Chunk {",
        "  uint32_t first;",
        "  uint8_t count;",
        "  uint8_t layer;",
        "  uint16_t min_x;",
        "  uint16_t min_y;",
        "  uint16_t max_x;",
        "  uint16_t max_y;",
        "};",
        "",
        f"constexpr float kLonMin = {LON_MIN}f;",
        f"constexpr float kLonSpan = {LON_MAX - LON_MIN}f;",
        f"constexpr float kLatMin = {LAT_MIN}f;",
        f"constexpr float kLatSpan = {LAT_MAX - LAT_MIN:.6g}f;",
        "",
        f"constexpr size_t kPointCount = {point_count};",
        f"constexpr size_t kChunkCount = {chunk_count};",
        f"constexpr size_t kTriangleCount = {tri_count};",
        f"constexpr size_t kFillChunkCount = {fill_chunk_count};",
        "",
        "extern const Point kPoints[];",
        "extern const Chunk kChunks[];",
        "extern const Triangle kTriangles[];",
        "extern const Chunk kFillChunks[];",
        "",
        "}  // namespace data::terrain",
        "",
    ])


def render_cpp(points, chunks, tris, fill_chunks) -> str:
    lines = [
        "// Generated by scripts/build_terrain_outlines.py — do not edit.",
        "// Data: Natural Earth (public domain), naturalearthdata.com",
        '#include "data/terrain_outlines.h"',
        "",
        "namespace data::terrain {",
        "",
        "const Point kPoints[] = {",
    ]
    for i in range(0, len(points), 8):
        row = points[i:i + 8]
        lines.append("  " + " ".join(f"{{{x},{y}}}," for x, y in row))
    lines += ["};", "", "const Chunk kChunks[] = {"]
    for first, count, layer, x0, y0, x1, y1 in chunks:
        lines.append(f"  {{{first}, {count}, {layer}, {x0}, {y0}, {x1}, {y1}}},")
    lines += ["};", "", "const Triangle kTriangles[] = {"]
    for a, b, c in tris:
        lines.append(f"  {{{{{a[0]},{a[1]}}}, {{{b[0]},{b[1]}}}, {{{c[0]},{c[1]}}}}},")
    lines += ["};", "", "const Chunk kFillChunks[] = {"]
    for first, count, layer, x0, y0, x1, y1 in fill_chunks:
        lines.append(f"  {{{first}, {count}, {layer}, {x0}, {y0}, {x1}, {y1}}},")
    lines += ["};", "", "}  // namespace data::terrain", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", type=Path, help="reuse/save downloaded GeoJSON here")
    args = ap.parse_args()

    points, chunks, per_layer = build_dataset(args.cache)
    tris, fill_chunks, fill_per_layer = build_fill(args.cache)
    OUT_H.parent.mkdir(parents=True, exist_ok=True)
    OUT_CPP.parent.mkdir(parents=True, exist_ok=True)
    OUT_H.write_text(
        render_header(len(points), len(chunks), len(tris), len(fill_chunks)),
        encoding="utf-8")
    OUT_CPP.write_text(render_cpp(points, chunks, tris, fill_chunks), encoding="utf-8")
    names = {LAYER_RIVER: "river", LAYER_LAKE: "lake", LAYER_COAST: "coast",
             LAYER_BORDER: "border"}
    summary = ", ".join(f"{names[k]} {v}" for k, v in sorted(per_layer.items()))
    flash_kb = (len(points) * 4 + (len(chunks) + len(fill_chunks)) * 16 +
                len(tris) * 12) / 1024
    print(f"wrote {OUT_H.name} + {OUT_CPP.name}: {len(points)} points "
          f"({summary}), {len(chunks)} chunks; {len(tris)} triangles "
          f"(land {fill_per_layer.get(FILL_LAND, 0)}, "
          f"lake {fill_per_layer.get(FILL_LAKE, 0)}), {len(fill_chunks)} fill "
          f"chunks; ~{flash_kb:.0f} KB flash")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
