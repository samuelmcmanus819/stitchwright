#!/usr/bin/env python3
"""
png_to_embroidery.py — convert simple line-art PNGs into embroidery files
(VP3 + DST), running-stitch or satin.

WHAT THIS IS GOOD FOR
    Thin line art / script text / outline drawings with a handful of flat
    colors (logos, monograms, "made with love" style text, outline hearts,
    etc). It skeletonizes each color region to its centerline and stitches
    along it.

WHAT THIS IS *NOT* GOOD FOR
    Filled/solid shapes, photos, gradients. Those need real digitizing
    software (Ink/Stitch, SewArt, etc).

VP4 NOTE
    No open-source library can currently write native VP4. This writes VP3
    instead, which VP4-capable Husqvarna/Pfaff software reads natively.

DEPENDENCIES
    pip install pyembroidery scikit-image networkx numpy pillow scipy \
                --break-system-packages

USAGE (command line)
    python3 png_to_embroidery.py input.png --out design \
        --width-mm 120 --stitch-type satin --formats vp3,dst,pes

    python3 png_to_embroidery.py input.png --out design \
        --width-mm 120 --stitch-type running --stitch-len-mm 2.0

    Produces design.<fmt> for each requested format, plus design_preview.svg
    (a vector render of the stitch paths). Formats: vp3, dst, pes, jef, exp,
    hus, pec.

USAGE (as a library / from a web or desktop wrapper)
    from png_to_embroidery import convert, ConversionError
    result = convert(image_bytes_or_path, width_mm=120,
                     stitch_type="running", output_formats=["vp3", "dst"])
    # result["formats"]["vp3"] -> bytes ; result["preview_svg"] -> SVG string
    # result["stats"] -> stitch_count, trim_count, colors, dimensions, ...
    convert() never touches the filesystem, so it runs unchanged in the
    browser under Pyodide.

HOW IT WORKS
    1. Cluster non-background pixels into thread colors (k-means, with a
       merge step that collapses anti-aliasing halos back into their real
       color instead of inventing a spurious extra thread).
    2. For each color: skeletonize to 1px centerlines; prune tiny spurs;
       merge pixel-clusters at intersections into single junction nodes.
    3. Decompose each connected skeleton component into a small graph
       (nodes = junctions/endpoints, edges = stroke fragments between them).
    4. ROUTE each component with an Eulerian circuit (networkx eulerize +
       eulerian_circuit): this finds a single continuous path that covers
       every fragment, retracing a minimal number of edges only where the
       topology truly requires it (e.g. a 3-way letter crossing). This is
       what keeps trims to ~one per physically-separate ink shape instead of
       one per self-crossing — earlier versions of this script trimmed at
       every letter self-crossing, which is both visually broken and a real
       cause of skipped stitches / gaps on actual fabric.
    5. Running stitch: resample the routed centerline to even spacing.
       Satin stitch: use the mask's distance transform to get local
       half-width at each centerline point, and zigzag between the two
       rails (perpendicular offsets) at a fine step — this is the standard
       "column from centerline + width" approach to auto-satin.
    6. Add small lock stitches (tie-in/tie-off) at every jump/trim boundary,
       matching normal digitizing practice, so the thread anchors properly
       instead of risking a skipped first stitch.
    7. Order the per-component runs with a greedy nearest-neighbor pass to
       reduce travel, then emit a pyembroidery pattern and write VP3+DST.

LIMITATIONS / KNOWN WEAK SPOTS
    - Satin width comes from the medial-axis distance transform, which
      assumes a roughly-uniform stroke; extremely tapered or blobby shapes
      may satin unevenly.
    - Very fine detail (small loops/dots much smaller than the stitch
      length) can still be lost in simplification.
    - Color clustering can misjudge busy/noisy source art; for tricky
      source images, flatten to clean solid colors first.
    - Eulerize retraces some fragments where genuinely required by topology
      (e.g. 3-way crossings) — this is normal in real digitizing, not a bug.
    - Always test-stitch on scrap fabric before running on a final piece.
"""

import argparse
import io
import sys
from collections import defaultdict

import numpy as np
from PIL import Image

from core.errors import StitchwrightError
from core.imaging import rewind

# Output formats we can emit. Each value is the pyembroidery writer function
# name; they are imported lazily so that importing this module (e.g. under
# Pyodide, before micropip has installed pyembroidery) stays cheap.
SUPPORTED_FORMATS = {
    "vp3": "write_vp3",
    "dst": "write_dst",
    "pes": "write_pes",
    "jef": "write_jef",
    "exp": "write_exp",
    "hus": "write_hus",
    "pec": "write_pec",
}

DEFAULT_FORMATS = ("vp3", "dst")

# Safety cap on total input-image pixels. skeleton_to_graph() puts one
# networkx node per skeleton pixel and extract_color_masks() k-means-clusters
# every ink pixel; both scale badly on large/complex images with no natural
# ceiling otherwise. ~16 megapixels comfortably covers real line-art/logo
# source images (this tool's intended input) with headroom.
MAX_IMAGE_PIXELS = 16_000_000

# max_colors controls k-means cluster count; min_spur_len feeds pruning-loop
# iteration counts. Both are unbounded from the CLI/library API (only the web
# UI clamps them client-side), so cap them here too.
MAX_ALLOWED_COLORS = 12
MAX_ALLOWED_SPUR_LEN = 500

# route_fragments_eulerian() calls networkx.eulerize() per connected skeleton
# component, which is roughly O(k^2 * (V+E) + k^3) in the number of
# odd-degree ("junction") nodes k (all-pairs shortest paths + max-weight
# matching over them). A busy/high-detail image can produce components with
# hundreds of junctions, which can hang or exhaust memory. Above this many
# odd-degree nodes, skip eulerization for that component and emit each
# fragment as its own stitch run instead (more trims, but bounded cost).
EULERIZE_ODD_NODE_CAP = 60

# order_runs_greedy() now orders runs in O(n log n) (k-d tree), so this is a
# generous sanity ceiling rather than the primary defense — it exists so a
# truly pathological (e.g. photographic) image fails fast with a clear
# message instead of consuming unbounded memory building fragments/graphs
# for a color that was never going to be stitchable line art anyway.
MAX_FRAGMENTS_PER_COLOR = 20_000


def log(msg):
    print(f"[png_to_embroidery] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Color detection
# ---------------------------------------------------------------------------

def extract_color_masks(image_source, max_colors=4, bg_white_thresh=245, min_pixel_frac=0.002):
    """image_source may be a filesystem path or any file-like object
    (e.g. io.BytesIO wrapping uploaded bytes)."""
    from scipy.cluster.vq import kmeans2

    img = Image.open(image_source)
    if img.width * img.height > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"Image is {img.width}x{img.height} ({img.width * img.height:,} px) — too large "
            f"to process safely (limit {MAX_IMAGE_PIXELS:,} px). Downscale the image first."
        )
    img = img.convert("RGBA")
    arr = np.array(img)
    alpha = arr[:, :, 3].astype(float) / 255.0
    rgb = arr[:, :, :3].astype(float)
    white = np.ones_like(rgb) * 255
    comp = rgb * alpha[..., None] + white * (1 - alpha[..., None])

    is_ink = (alpha > 0.3) & (comp.max(axis=-1) < bg_white_thresh)
    ink_pixels = comp[is_ink]
    if len(ink_pixels) == 0:
        raise ValueError("No non-background ink pixels found in image.")

    n_unique = len(np.unique(ink_pixels.round(), axis=0))
    n_clusters = min(max_colors + 2, max(1, n_unique), len(ink_pixels))

    # k-means++ init with a fixed seed keeps results deterministic. scipy's
    # kmeans2 replaces scikit-learn here purely to shrink the browser payload
    # (sklearn is one of the heaviest Pyodide packages); output is equivalent
    # for the handful-of-flat-colors case this tool targets.
    centers, labels = kmeans2(
        ink_pixels.astype(np.float64), n_clusters, minit="++", seed=0, iter=25,
    )
    labels_full = np.full(is_ink.shape, -1)
    labels_full[is_ink] = labels

    total_ink = is_ink.sum()
    raw = []
    for c in range(n_clusters):
        mask = labels_full == c
        cnt = int(mask.sum())
        if not cnt or cnt / total_ink < min_pixel_frac:
            continue
        raw.append([centers[c], mask, cnt])
    raw.sort(key=lambda cm: -cm[2])

    accepted = []
    white_v = np.array([255.0, 255.0, 255.0])
    for color, mask, cnt in raw:
        merged = False
        for i, (acc_color, acc_mask, acc_cnt) in enumerate(accepted):
            line = acc_color - white_v
            line_len2 = np.dot(line, line)
            if line_len2 < 1e-6:
                continue
            t = np.dot(color - white_v, line) / line_len2
            proj = white_v + t * line
            perp_dist = np.linalg.norm(color - proj)
            if 0.05 < t < 1.05 and perp_dist < 18:
                accepted[i] = (acc_color, acc_mask | mask, acc_cnt + cnt)
                merged = True
                break
        if not merged:
            accepted.append((color, mask, cnt))

    accepted.sort(key=lambda cm: -cm[2])
    accepted = accepted[:max_colors]
    masks = [(tuple(int(v) for v in color), mask) for color, mask, _ in accepted]
    log(f"Detected {len(masks)} color group(s): {[c for c, _ in masks]}")
    return masks, arr.shape[:2]


# ---------------------------------------------------------------------------
# Skeleton graph
# ---------------------------------------------------------------------------

def skeleton_to_graph(mask):
    import networkx as nx
    from skimage.morphology import skeletonize
    import math

    skel = skeletonize(mask)
    ys, xs = np.nonzero(skel)
    coords = set(zip(ys.tolist(), xs.tolist()))
    G = nx.Graph()
    for (y, x) in coords:
        G.add_node((y, x))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                n = (y + dy, x + dx)
                if n in coords:
                    G.add_edge((y, x), n, weight=math.hypot(dy, dx))
    return G, skel


def prune_spurs(G, min_len=15):
    G = G.copy()
    changed = True
    while changed:
        changed = False
        degrees = dict(G.degree())
        endpoints = [n for n, d in degrees.items() if d == 1]
        for ep in endpoints:
            if ep not in G:
                continue
            path = [ep]
            cur, prev = ep, None
            while True:
                nbrs = [n for n in G.neighbors(cur) if n != prev]
                if len(nbrs) != 1:
                    break
                nxt = nbrs[0]
                path.append(nxt)
                prev, cur = cur, nxt
                if degrees.get(cur, 0) != 2:
                    break
            end_deg = degrees.get(path[-1], 0)
            if end_deg >= 3 and len(path) < min_len:
                G.remove_nodes_from(path[:-1])
                changed = True
    return G


def merge_close_junctions(G):
    """Contract every edge whose both endpoints are junctions (degree >= 3)
    into a single node, repeating since a contraction can create new
    degree-3+ pairs. Uses an incremental candidate queue and mutates one
    graph in place, rather than rescanning every edge and deep-copying the
    whole graph per merge — on a busy/high-detail skeleton (many junctions)
    that full-rescan-per-merge approach is roughly O(junctions * edges) and
    can take tens of seconds to minutes; this is amortized O(V + E)."""
    from collections import deque

    G = G.copy()
    queued = set()
    queue = deque()

    def maybe_queue(u, v):
        if G.degree(u) >= 3 and G.degree(v) >= 3:
            key = (u, v) if u <= v else (v, u)
            if key not in queued:
                queued.add(key)
                queue.append(key)

    for u, v in G.edges():
        maybe_queue(u, v)

    while queue:
        u, v = queue.popleft()
        queued.discard((u, v))
        if u not in G or v not in G or not G.has_edge(u, v):
            continue
        if G.degree(u) < 3 or G.degree(v) < 3:
            continue
        for n in list(G.neighbors(v)):
            if n != u and not G.has_edge(u, n):
                G.add_edge(u, n)
        G.remove_node(v)
        for n in G.neighbors(u):
            maybe_queue(u, n)
    return G


def extract_fragments(G):
    """Decompose skeleton graph into simple-path fragments between
    junctions/endpoints. These become edges of a small routing graph."""
    import networkx as nx

    fragments = []
    for comp in nx.connected_components(G):
        sub = G.subgraph(comp).copy()
        if len(sub) < 3:
            continue
        degrees = dict(sub.degree())
        endpoints = [n for n, d in degrees.items() if d == 1]
        junctions = [n for n, d in degrees.items() if d >= 3]
        visited_edges = set()

        def edge_key(a, b):
            return (a, b) if a < b else (b, a)

        def walk_from(start):
            results = []
            for nbr in list(sub.neighbors(start)):
                ek = edge_key(start, nbr)
                if ek in visited_edges:
                    continue
                path = [start, nbr]
                visited_edges.add(ek)
                cur, prev = nbr, start
                while True:
                    d = degrees[cur]
                    if d != 2 or cur in endpoints:
                        break
                    nbrs = [n for n in sub.neighbors(cur) if n != prev]
                    if not nbrs:
                        break
                    nxt = nbrs[0]
                    ek2 = edge_key(cur, nxt)
                    if ek2 in visited_edges:
                        break
                    visited_edges.add(ek2)
                    path.append(nxt)
                    prev, cur = cur, nxt
                results.append(path)
            return results

        starts = endpoints + junctions if (endpoints or junctions) else list(sub.nodes())[:1]
        for s in starts:
            for p in walk_from(s):
                if len(p) >= 2:
                    fragments.append(p)

        remaining = [e for e in sub.edges() if edge_key(*e) not in visited_edges]
        while remaining:
            e = remaining[0]
            path = [e[0], e[1]]
            visited_edges.add(edge_key(*e))
            cur, prev = e[1], e[0]
            while True:
                nbrs = [n for n in sub.neighbors(cur) if n != prev and edge_key(cur, n) not in visited_edges]
                if not nbrs:
                    break
                nxt = nbrs[0]
                visited_edges.add(edge_key(cur, nxt))
                path.append(nxt)
                prev, cur = cur, nxt
                if cur == path[0]:
                    break
            fragments.append(path)
            remaining = [e for e in sub.edges() if edge_key(*e) not in visited_edges]
    return fragments


# ---------------------------------------------------------------------------
# Eulerian routing: turns fragments into as few continuous runs as topology
# allows, instead of trimming at every self-crossing.
# ---------------------------------------------------------------------------

def route_fragments_eulerian(fragments):
    """fragments: list of pixel-paths (each a list of (y,x)).
    Returns: list of routed pixel-paths, one per connected component,
    each a single continuous walk covering every fragment in it at least
    once (retracing only where the topology requires it)."""
    import networkx as nx

    MG = nx.MultiGraph()
    for idx, frag in enumerate(fragments):
        u, v = frag[0], frag[-1]
        MG.add_edge(u, v, idx=idx)

    pair_lookup = defaultdict(list)
    for idx, frag in enumerate(fragments):
        pair_lookup[frozenset((frag[0], frag[-1]))].append(idx)

    routed = []
    for comp_nodes in nx.connected_components(MG):
        sub = MG.subgraph(comp_nodes).copy()
        if sub.number_of_edges() == 0:
            continue

        odd_nodes = [n for n, d in sub.degree() if d % 2 == 1]
        if not nx.is_eulerian(sub) and len(odd_nodes) > EULERIZE_ODD_NODE_CAP:
            # Too many junctions to eulerize safely (see EULERIZE_ODD_NODE_CAP) —
            # fall back to one run per fragment instead of a single routed
            # circuit. More trims, but bounded cost on busy/complex art.
            log(
                f"  component has {len(odd_nodes)} junctions (>{EULERIZE_ODD_NODE_CAP}); "
                "skipping eulerization, routing fragments separately"
            )
            for u, v, k, data in sub.edges(keys=True, data=True):
                idx = data.get("idx")
                if idx is None:
                    continue
                frag = fragments[idx]
                if len(frag) >= 2:
                    routed.append(frag)
            continue

        sub_e = sub if nx.is_eulerian(sub) else nx.eulerize(sub)
        circuit = list(nx.eulerian_circuit(sub_e, keys=True, source=list(sub_e.nodes())[0]))

        path_pts = []
        for (u, v, k) in circuit:
            data = sub_e.get_edge_data(u, v, k) or {}
            idx = data.get("idx")
            if idx is None:
                cands = pair_lookup.get(frozenset((u, v)))
                if not cands:
                    continue
                idx = cands[0]
            frag = fragments[idx]
            oriented = frag if frag[0] == u else frag[::-1]
            if path_pts and path_pts[-1] == oriented[0]:
                path_pts.extend(oriented[1:])
            else:
                path_pts.extend(oriented)
        if len(path_pts) >= 2:
            routed.append(path_pts)
    return routed


# ---------------------------------------------------------------------------
# Running-stitch resampling
# ---------------------------------------------------------------------------

def simplify_and_resample(path_px, scale_mm_per_px, stitch_len_mm, tolerance_px=1.0):
    from skimage.measure import approximate_polygon

    pts = np.array([(x, y) for (y, x) in path_px], dtype=float)
    if len(pts) < 2:
        return None
    simp = approximate_polygon(pts, tolerance=tolerance_px)
    if len(simp) < 2:
        simp = pts[[0, -1]]
    seg_lengths = np.linalg.norm(np.diff(simp, axis=0), axis=1)
    total_len_px = seg_lengths.sum()
    if total_len_px == 0:
        return None
    step_px = stitch_len_mm / scale_mm_per_px
    n_steps = max(1, int(round(total_len_px / step_px)))
    cum = np.concatenate([[0], np.cumsum(seg_lengths)])
    targets = np.linspace(0, total_len_px, n_steps + 1)
    out, seg_idx = [], 0
    for t in targets:
        while seg_idx < len(cum) - 2 and cum[seg_idx + 1] < t:
            seg_idx += 1
        seg_total = seg_lengths[seg_idx] if seg_lengths[seg_idx] > 0 else 1e-9
        frac = min(max((t - cum[seg_idx]) / seg_total, 0), 1)
        out.append(simp[seg_idx] + frac * (simp[seg_idx + 1] - simp[seg_idx]))
    return np.array(out)


# ---------------------------------------------------------------------------
# Satin generation: centerline + local half-width (medial-axis distance
# transform) -> zigzag between the two rails.
# ---------------------------------------------------------------------------

def generate_satin(path_px, mask, scale_mm_per_px, satin_step_mm=0.4, width_scale=0.9):
    from scipy.ndimage import distance_transform_edt

    dist = distance_transform_edt(mask)
    pts = np.array([(x, y) for (y, x) in path_px], dtype=float)  # (x,y)
    if len(pts) < 3:
        return None

    seg_lengths = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    total_len_px = seg_lengths.sum()
    if total_len_px == 0:
        return None
    step_px = max(satin_step_mm / scale_mm_per_px, 0.75)
    n_steps = max(2, int(round(total_len_px / step_px)))
    cum = np.concatenate([[0], np.cumsum(seg_lengths)])
    targets = np.linspace(0, total_len_px, n_steps + 1)

    centers, tangents = [], []
    seg_idx = 0
    for t in targets:
        while seg_idx < len(cum) - 2 and cum[seg_idx + 1] < t:
            seg_idx += 1
        seg_total = seg_lengths[seg_idx] if seg_lengths[seg_idx] > 0 else 1e-9
        frac = min(max((t - cum[seg_idx]) / seg_total, 0), 1)
        c = pts[seg_idx] + frac * (pts[seg_idx + 1] - pts[seg_idx])
        tang = pts[seg_idx + 1] - pts[seg_idx]
        centers.append(c)
        tangents.append(tang)

    centers = np.array(centers)
    tangents = np.array(tangents)
    norms = np.linalg.norm(tangents, axis=1)
    norms[norms == 0] = 1.0
    tangents = tangents / norms[:, None]
    perps = np.stack([-tangents[:, 1], tangents[:, 0]], axis=1)

    h, w = mask.shape
    rail_a, rail_b = [], []
    for c, p in zip(centers, perps):
        xi = int(round(np.clip(c[0], 0, w - 1)))
        yi = int(round(np.clip(c[1], 0, h - 1)))
        half_w = max(dist[yi, xi] * width_scale, 1.0)
        rail_a.append(c + p * half_w)
        rail_b.append(c - p * half_w)

    out = []
    for i in range(len(centers)):
        out.append(rail_a[i] if i % 2 == 0 else rail_b[i])
    return np.array(out)


# ---------------------------------------------------------------------------
# Per-color processing
# ---------------------------------------------------------------------------

def process_color_mask(mask, scale_mm_per_px, stitch_type, stitch_len_mm, satin_step_mm,
                        min_spur_len=15):
    G, _ = skeleton_to_graph(mask)
    G = prune_spurs(G, min_len=min_spur_len)
    G = merge_close_junctions(G)
    G = prune_spurs(G, min_len=min_spur_len)
    fragments = extract_fragments(G)
    if len(fragments) > MAX_FRAGMENTS_PER_COLOR:
        raise ValueError(
            f"artwork is too detailed for this tool ({len(fragments)} distinct strokes in one "
            f"color, limit {MAX_FRAGMENTS_PER_COLOR}) — this converter targets thin line art, "
            "not busy/noisy or photographic images; simplify or flatten the source image"
        )
    routed = route_fragments_eulerian(fragments)

    runs = []
    for path_px in routed:
        if stitch_type == "satin":
            pts = generate_satin(path_px, mask, scale_mm_per_px, satin_step_mm=satin_step_mm)
        else:
            pts = simplify_and_resample(path_px, scale_mm_per_px, stitch_len_mm)
        if pts is not None and len(pts) >= 2:
            runs.append([pts])  # one "run" = list of one segment (already continuous)
    return runs


def order_runs_greedy(runs):
    """Greedy nearest-neighbor ordering to reduce travel between runs.

    Uses a k-d tree over all run endpoints with soft deletion (an expanding-k
    query that skips already-used points) instead of a naive O(n^2) scan of
    every remaining run at every step. That naive version is fine for a
    handful of strokes but became the dominant cost on real multi-thousand-
    stroke designs — e.g. ~9300 runs took ~4 minutes in it alone. This is
    O(n log n) in the typical case."""
    n = len(runs)
    if n <= 1:
        return runs

    from scipy.spatial import cKDTree

    starts = np.array([r[0][0] for r in runs])
    ends = np.array([r[-1][-1] for r in runs])
    pts = np.vstack([starts, ends])  # index i -> start of run i; index n+i -> end of run i
    tree = cKDTree(pts)
    removed = np.zeros(2 * n, dtype=bool)

    ordered, reversed_flags = [], []
    cur_pos = np.array([0.0, 0.0])

    for _ in range(n):
        k = 1
        chosen = None
        while chosen is None:
            k = min(k, 2 * n)
            _, idxs = tree.query(cur_pos, k=k)
            idxs = np.atleast_1d(idxs)
            for idx in idxs:
                if not removed[idx]:
                    chosen = int(idx)
                    break
            if chosen is None:
                if k >= 2 * n:
                    break
                k *= 4
        run_i = chosen % n
        rev = chosen >= n
        ordered.append(run_i)
        reversed_flags.append(rev)
        removed[run_i] = True
        removed[run_i + n] = True
        cur_pos = starts[run_i] if rev else ends[run_i]

    return [
        ([seg[::-1] for seg in reversed(runs[i])] if rev else runs[i])
        for i, rev in zip(ordered, reversed_flags)
    ]


# ---------------------------------------------------------------------------
# Pattern assembly
# ---------------------------------------------------------------------------

def add_lock_stitch(pattern, x, y):
    """Tiny tie-in/tie-off: tack a couple of ~0.3mm stitches in place so the
    thread anchors instead of risking a skipped/pulled first or last
    stitch at a jump or trim boundary."""
    jitter = 3.0  # 0.1mm units -> ~0.3mm
    pattern.stitch_abs(x + jitter, y)
    pattern.stitch_abs(x - jitter, y + jitter)
    pattern.stitch_abs(x, y)


def build_pattern(color_runs, scale_mm_per_px, lock_stitches=True):
    from pyembroidery import EmbPattern, EmbThread

    pattern = EmbPattern()
    first_color = True
    for color, runs in color_runs:
        if not runs:
            continue
        r, g, b = color
        pattern.add_thread(EmbThread(thread=(r << 16) | (g << 8) | b))
        if not first_color:
            pattern.color_change()
        first_run = True
        for run in runs:
            all_pts = np.vstack(run)
            x0, y0 = all_pts[0] * scale_mm_per_px * 10.0
            if not (first_color and first_run):
                pattern.trim()
            pattern.move_abs(x0, y0)
            if lock_stitches:
                add_lock_stitch(pattern, x0, y0)
            for pt in all_pts[1:]:
                x, y = pt * scale_mm_per_px * 10.0
                pattern.stitch_abs(x, y)
            if lock_stitches:
                xl, yl = all_pts[-1] * scale_mm_per_px * 10.0
                add_lock_stitch(pattern, xl, yl)
            first_run = False
        first_color = False
    pattern.end()
    return pattern


# ---------------------------------------------------------------------------
# Public conversion API
#
# convert() is the single entry point wrappers (web UI, desktop GUI, CLI)
# should call. It takes image bytes or a path plus keyword options, and
# returns finished file bytes + a preview + stats — it never touches the
# filesystem itself, so it works unchanged in a browser under Pyodide.
# ---------------------------------------------------------------------------

class ConversionError(StitchwrightError):
    """A problem worth showing the user verbatim: unreadable image, no
    stitchable line art, or an unsupported output format."""


def _pattern_to_bytes(pattern, writer_name):
    import pyembroidery

    writer = getattr(pyembroidery, writer_name, None)
    if writer is None:
        raise ConversionError(f"This pyembroidery build cannot write via {writer_name}().")
    buf = io.BytesIO()
    writer(pattern, buf)
    return buf.getvalue()


def _render_preview_svg(pattern):
    """Vector render of the finished stitch data: one <path> per stitch block,
    stroked in its thread colour (pyembroidery's own SVG writer). Rendered from
    the in-memory pattern — i.e. the exact stitch points that get written to the
    VP3/DST, at full precision, before the file format's ~0.1 mm coordinate
    rounding (which is invisible in thread but would show as jitter here)."""
    import re
    import pyembroidery

    writer = getattr(pyembroidery, "write_svg", None)
    if writer is None:
        log("preview unavailable: this pyembroidery build has no write_svg()")
        return None
    buf = io.BytesIO()
    try:
        writer(pattern, buf)
    except Exception as exc:  # preview is best-effort, never fatal
        log(f"preview render failed: {exc}")
        return None
    svg = buf.getvalue().decode("utf-8", "replace")
    # Drop the fixed pixel width/height so the host can scale it freely; the
    # viewBox carries the real proportions.
    svg = re.sub(r'(<svg\b[^>]*?)\s+width="[^"]*"\s+height="[^"]*"', r"\1", svg, count=1)
    return svg


def convert(
    image_source,
    *,
    width_mm=120.0,
    stitch_type="running",
    stitch_len_mm=2.0,
    satin_step_mm=0.4,
    max_colors=4,
    min_spur_len=15,
    lock_stitches=True,
    output_formats=DEFAULT_FORMATS,
    make_preview=True,
):
    """Convert line-art image bytes/path into embroidery files.

    image_source: a filesystem path, or any file-like object (e.g.
        io.BytesIO around uploaded bytes).

    Returns:
        {
          "formats":     {"vp3": b"...", "dst": b"...", ...},
          "preview_svg":  "<svg ...>...</svg>" | None,
          "stats": {
              "stitch_count", "trim_count", "jump_count",
              "color_count", "colors", "run_count",
              "width_mm", "height_mm",
          },
        }

    Raises ConversionError for anything the end user should see.
    """
    from PIL import UnidentifiedImageError

    fmts = [f.lower().lstrip(".") for f in output_formats]
    if not fmts:
        raise ConversionError("Pick at least one output format.")
    unknown = [f for f in fmts if f not in SUPPORTED_FORMATS]
    if unknown:
        raise ConversionError(
            f"Unsupported output format(s): {', '.join(unknown)}. "
            f"Choose from: {', '.join(sorted(SUPPORTED_FORMATS))}."
        )
    if stitch_type not in ("running", "satin"):
        raise ConversionError("stitch_type must be 'running' or 'satin'.")
    if width_mm <= 0:
        raise ConversionError("width_mm must be positive.")
    if not (1 <= max_colors <= MAX_ALLOWED_COLORS):
        raise ConversionError(f"max_colors must be between 1 and {MAX_ALLOWED_COLORS}.")
    if not (0 <= min_spur_len <= MAX_ALLOWED_SPUR_LEN):
        raise ConversionError(f"min_spur_len must be between 0 and {MAX_ALLOWED_SPUR_LEN}.")

    rewind(image_source)

    try:
        masks, (img_h, img_w) = extract_color_masks(image_source, max_colors=max_colors)
    except (ValueError, UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ConversionError(f"Could not process image: {exc}") from exc

    scale_mm_per_px = width_mm / img_w

    color_runs = []
    total_runs = 0
    skipped = []
    for color, mask in masks:
        log(f"Processing color {color} ({int(mask.sum())} px)...")
        try:
            runs = process_color_mask(
                mask, scale_mm_per_px, stitch_type, stitch_len_mm, satin_step_mm,
                min_spur_len=min_spur_len,
            )
        except Exception as exc:  # noqa: BLE001 - isolate one bad color, not the whole image
            log(f"  -> skipped color {color}: {exc}")
            skipped.append((color, str(exc)))
            color_runs.append((color, []))
            continue
        runs = order_runs_greedy(runs)
        total_runs += len(runs)
        log(f"  -> {len(runs)} continuous stitch run(s)")
        color_runs.append((color, runs))

    if total_runs == 0:
        if skipped:
            raise ConversionError(
                "Could not stitch this image: " + "; ".join(msg for _, msg in skipped)
            )
        raise ConversionError(
            "No stitchable strokes found. This tool expects thin line art; "
            "filled or photographic images need real digitizing software."
        )

    pattern = build_pattern(color_runs, scale_mm_per_px, lock_stitches=lock_stitches)

    from pyembroidery import TRIM, JUMP

    formats = {f: _pattern_to_bytes(pattern, SUPPORTED_FORMATS[f]) for f in fmts}
    preview_svg = _render_preview_svg(pattern) if make_preview else None

    cmds = [s[2] for s in pattern.stitches]
    used = [(color, runs) for color, runs in color_runs if runs]
    stats = {
        "stitch_count": pattern.count_stitches(),
        "trim_count": sum(1 for c in cmds if c == TRIM),
        "jump_count": sum(1 for c in cmds if c == JUMP),
        "color_count": len(used),
        "colors": [list(color) for color, _ in used],
        "run_count": total_runs,
        "width_mm": round(width_mm, 2),
        "height_mm": round(img_h * scale_mm_per_px, 2),
    }
    log(
        f"Done: {stats['stitch_count']} stitches, {stats['trim_count']} trims, "
        f"{stats['color_count']} color(s), {stats['width_mm']}x{stats['height_mm']} mm"
    )
    return {"formats": formats, "preview_svg": preview_svg, "stats": stats}


# ---------------------------------------------------------------------------
# Main (thin CLI wrapper around convert())
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_png")
    ap.add_argument("--out", default="design", help="output filename prefix")
    ap.add_argument("--width-mm", type=float, default=120.0, help="design width in mm")
    ap.add_argument("--stitch-type", choices=["running", "satin"], default="running")
    ap.add_argument("--stitch-len-mm", type=float, default=2.0, help="running-stitch spacing in mm")
    ap.add_argument("--satin-step-mm", type=float, default=0.4, help="satin zigzag step in mm")
    ap.add_argument("--max-colors", type=int, default=4, help="max distinct thread colors to detect")
    ap.add_argument("--min-spur-len", type=int, default=15, help="skeleton spur-pruning threshold in px")
    ap.add_argument("--no-lock-stitches", action="store_true", help="disable tie-in/tie-off lock stitches")
    ap.add_argument(
        "--formats",
        default=",".join(DEFAULT_FORMATS),
        help="comma-separated output formats (" + ", ".join(sorted(SUPPORTED_FORMATS)) + ")",
    )
    ap.add_argument("--no-preview", action="store_true", help="skip the PNG preview render")
    args = ap.parse_args()

    fmts = [f.strip() for f in args.formats.split(",") if f.strip()]
    try:
        result = convert(
            args.input_png,
            width_mm=args.width_mm,
            stitch_type=args.stitch_type,
            stitch_len_mm=args.stitch_len_mm,
            satin_step_mm=args.satin_step_mm,
            max_colors=args.max_colors,
            min_spur_len=args.min_spur_len,
            lock_stitches=not args.no_lock_stitches,
            output_formats=fmts,
            make_preview=not args.no_preview,
        )
    except ConversionError as exc:
        log(f"error: {exc}")
        sys.exit(1)

    written = []
    for ext, data in result["formats"].items():
        path = f"{args.out}.{ext}"
        with open(path, "wb") as fh:
            fh.write(data)
        written.append(path)
    if result["preview_svg"]:
        path = f"{args.out}_preview.svg"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(result["preview_svg"])
        written.append(path)

    log("Wrote " + ", ".join(written))


if __name__ == "__main__":
    main()
