#!/usr/bin/env python3
"""
pattern_tiler.py — arrange one or more design images onto a canvas sized for
a specific print-on-demand product (mug wrap, tote bag, sticker sheet, ...).

WHAT THIS IS GOOD FOR
    Filling a fixed-size print canvas with one or more small design/tile
    images without manually resizing and repositioning them — sticker
    sheets, repeat-pattern fabric yardage, all-over placements. This is the
    layout companion to png_to_embroidery.py: same "one Python module, no
    filesystem, runs unchanged under Pyodide" shape, different job.

WHAT THIS IS *NOT* FOR
    Digitizing anything for stitching — that's png_to_embroidery.py. This
    module only composites raster images onto a raster canvas.

LAYOUT MODES
    grid     — designs cycle round-robin through a fixed grid of cells
               across the canvas (sticker sheets, alternating patterns).
    scatter  — designs at randomized positions, sizes and rotations, placed
               with a retry loop and a bounding-circle overlap check so
               nothing collides (see _layout_scatter()).
    seamless — designs arranged once into a small repeat unit (a "motif"),
               each kept fully inside its own cell (never touching the
               motif's own edge), then that motif is tiled edge-to-edge
               across the whole canvas — the CSS background-repeat
               principle: since nothing inside the motif crosses its
               border, adjacent copies always meet at the same padding, so
               there's no seam (see _layout_seamless()).

USAGE (command line)
    python3 pattern_tiler.py sticker1.png sticker2.png \
        --preset sticker_sheet --layout grid --out sheet.png

    python3 pattern_tiler.py logo.png \
        --width-in 12 --height-in 12 --dpi 300 --columns 3 --out grid.png

USAGE (as a library / from a web or desktop wrapper)
    from pattern_tiler import convert, TilerError
    result = convert(["a.png", "b.png"], preset="sticker_sheet")
    # result["image_png"]  -> bytes, full-resolution composited canvas
    # result["preview_png"] -> bytes, downscaled for on-screen display
    # result["stats"]      -> canvas size, DPI, placement counts, ...
    convert() never touches the filesystem, so it runs unchanged in the
    browser under Pyodide.
"""

import argparse
import io
import sys

from core.errors import StitchwrightError
from core.imaging import load_rgba
from core.presets import PRODUCT_PRESETS, MAX_CANVAS_PIXELS, resolve_canvas
from core.units import in_to_px

LAYOUT_MODES = ("grid", "scatter", "seamless")

DEFAULT_MARGIN_IN = 0.125
DEFAULT_SPACING_IN = 0.125
DEFAULT_COLUMNS = 4
PREVIEW_MAX_PX = 1400


def log(msg):
    print(f"[pattern_tiler] {msg}", file=sys.stderr)


class TilerError(StitchwrightError):
    """A problem worth showing the user verbatim: no images given, an
    unknown preset/layout mode, or a canvas too large to render safely."""


# ---------------------------------------------------------------------------
# Input normalization
# ---------------------------------------------------------------------------

def _normalize_images(images):
    """images: a list of sources (path / file-like), or a list of dicts
    {"source": ..., "name": optional str, "scale": optional float,
    "lock_rotation": optional bool}. Returns a list of normalized dicts with
    a loaded PIL RGBA image under "image"."""
    if not images:
        raise TilerError("Add at least one design image.")

    out = []
    for i, item in enumerate(images):
        spec = dict(item) if isinstance(item, dict) else {"source": item}
        source = spec.get("source")
        if source is None:
            raise TilerError(f"Design #{i + 1} has no image data.")
        name = spec.get("name") or f"design_{i + 1}"
        scale = float(spec.get("scale") or 1.0)
        if scale <= 0:
            raise TilerError(f"'{name}': scale must be positive.")
        try:
            img = load_rgba(source)
        except Exception as exc:
            raise TilerError(f"Could not read '{name}': {exc}") from exc
        out.append({
            "name": name,
            "image": img,
            "scale": scale,
            "lock_rotation": bool(spec.get("lock_rotation")),
        })
    return out


# ---------------------------------------------------------------------------
# Grid repeat layout
# ---------------------------------------------------------------------------

def _grid_cell_size(usable_w, usable_h, spacing_px, columns):
    """The cell size a `columns`-wide grid would use to tile usable_w×usable_h
    edge-to-edge, picking the row count that keeps cells roughly square (the
    reference "how big should one design be at this density" used by every
    layout mode). Returns (cell_w, cell_h, rows)."""
    columns = max(1, int(columns))
    cell_w = (usable_w - (columns - 1) * spacing_px) / columns
    if cell_w <= 0:
        raise TilerError("Too many columns (or too much spacing) for this canvas width.")
    rows = max(1, round((usable_h + spacing_px) / (cell_w + spacing_px)))
    cell_h = (usable_h - (rows - 1) * spacing_px) / rows
    if cell_h <= 0:
        raise TilerError("Too much spacing (or too many rows) for this canvas height.")
    return cell_w, cell_h, rows


def _layout_grid(images, canvas_w_px, canvas_h_px, *, margin_px, spacing_px, columns):
    """Cycle `images` round-robin through a fixed grid of square-ish cells
    spanning the canvas. Returns (placements, columns, rows), where each
    placement is {"index", "cell": (x, y, w, h)} — the cell each design is
    centered/contained within, not yet scaled to a specific image.

    Cells are stretched to fill usable_h exactly (see _grid_cell_size) so the
    grid tiles the whole canvas edge-to-edge instead of leaving a gap below
    the last row; each design is still fitted, not stretched, within its own
    cell."""
    usable_w = canvas_w_px - 2 * margin_px
    usable_h = canvas_h_px - 2 * margin_px
    if usable_w <= 0 or usable_h <= 0:
        raise TilerError("Margin leaves no room on the canvas — reduce it.")

    columns = max(1, int(columns))
    cell_w, cell_h, rows = _grid_cell_size(usable_w, usable_h, spacing_px, columns)

    placements = []
    n = len(images)
    for r in range(rows):
        for c in range(columns):
            idx = (r * columns + c) % n
            x = margin_px + c * (cell_w + spacing_px)
            y = margin_px + r * (cell_h + spacing_px)
            placements.append({"index": idx, "cell": (x, y, cell_w, cell_h)})
    return placements, columns, rows


def _layout_scatter(images, canvas_w_px, canvas_h_px, *, margin_px, spacing_px, columns,
                     design_scale, jitter, seed):
    """Randomized placement with collision avoidance: designs get a random
    position (and, via _composite's jitter/rotation_jitter_deg, a further
    random in-box drift/tilt) within the canvas, each tried against a retry
    loop that rejects any position whose bounding circle would overlap an
    already-placed design's. `columns` sets the target design count and
    reference size the same way it does for 'grid' (so the Density control
    means roughly the same thing in both modes); `jitter` adds size variety
    between designs (0 = all the same size, 1 = down to half).

    `design_scale` shrinks the reserved box *here*, at layout time — not
    only cosmetically at render time like it does for 'grid' — because
    unlike a grid's fixed cells, a smaller design here should free up real
    room for the retry loop to pack more of them in. The caller must
    therefore composite scatter's placements with design_scale=1.0 (already
    baked in) to avoid shrinking twice.

    Returns (placements, None, None) — matching _layout_grid's return shape,
    with columns/rows omitted since there's no fixed grid to report."""
    import math
    import random

    usable_w = canvas_w_px - 2 * margin_px
    usable_h = canvas_h_px - 2 * margin_px
    if usable_w <= 0 or usable_h <= 0:
        raise TilerError("Margin leaves no room on the canvas — reduce it.")

    cell_w, cell_h, rows = _grid_cell_size(usable_w, usable_h, spacing_px, columns)
    target_count = max(1, int(columns)) * rows

    rng = random.Random(seed)
    n = len(images)
    bag = []

    def next_index():
        nonlocal bag
        if not bag:
            bag = list(range(n))
            rng.shuffle(bag)
        return bag.pop()

    placed = []  # (cx, cy, radius)
    placements = []
    attempts_per_try = 25
    shrink_steps = 5  # each halves the miss rate roughly; keeps a crowded canvas usably full
    for _ in range(target_count):
        size_factor = rng.uniform(1 - 0.5 * jitter, 1.0) if jitter else 1.0
        base_box_w = max(4.0, cell_w * design_scale * size_factor)
        base_box_h = max(4.0, cell_h * design_scale * size_factor)

        found = False
        for shrink in range(shrink_steps):
            shrink_factor = 0.85 ** shrink
            box_w, box_h = base_box_w * shrink_factor, base_box_h * shrink_factor
            radius = math.hypot(box_w, box_h) / 2

            lo_x, hi_x = margin_px + box_w / 2, canvas_w_px - margin_px - box_w / 2
            lo_y, hi_y = margin_px + box_h / 2, canvas_h_px - margin_px - box_h / 2
            if hi_x < lo_x:
                lo_x = hi_x = (lo_x + hi_x) / 2
            if hi_y < lo_y:
                lo_y = hi_y = (lo_y + hi_y) / 2

            for _attempt in range(attempts_per_try):
                cx = rng.uniform(lo_x, hi_x)
                cy = rng.uniform(lo_y, hi_y)
                if all(math.hypot(cx - pcx, cy - pcy) >= radius + pr + spacing_px for pcx, pcy, pr in placed):
                    placed.append((cx, cy, radius))
                    placements.append(
                        {"index": next_index(), "cell": (cx - box_w / 2, cy - box_h / 2, box_w, box_h)}
                    )
                    found = True
                    break
            if found:
                break
        # else: even the smallest attempt couldn't find room — skip this one
        # rather than loop forever or overlap; placed_count in the result
        # will simply be lower than requested.

    if not placements:
        raise TilerError("Couldn't fit any designs on this canvas — try a smaller Density or more spacing.")
    return placements, None, None


def _place_once(images, w_px, h_px):
    """Place each of `images` exactly once (no repeats), packed into a
    roughly-square sub-grid that fills w_px×h_px edge-to-edge. Used to build
    one seamless-tile repeat unit, where each design should appear once."""
    import math

    n = len(images)
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = max(1, math.ceil(n / cols))
    cell_w = w_px / cols
    cell_h = h_px / rows
    return [
        {"index": i, "cell": ((i % cols) * cell_w, (i // cols) * cell_h, cell_w, cell_h)}
        for i in range(n)
    ]


def _layout_seamless(images, canvas_w_px, canvas_h_px, *, margin_px, columns, design_scale, background):
    """Build one small repeat unit ("motif") containing every design exactly
    once — each in its own cell, per _place_once, so no design ever touches
    the motif's own edge — then stamp that motif edge-to-edge across the
    canvas like a CSS background-repeat. Because nothing inside the motif
    crosses its border, adjacent copies always meet at that same padding, so
    the tiling has no seam without needing any edge-wrap trick — wrap-
    shifting the motif (Image.offset) would in fact do the opposite here: it
    would drag already-contained content onto the tile boundary and cut it
    in half there instead.

    `columns` sets how many times the motif repeats across the canvas width
    (the motif is square, so the same count roughly holds down its height).
    Unlike the other modes this composites the canvas itself and returns it
    directly, rather than a placements list for the caller to composite."""
    import math

    from PIL import Image

    usable_w = canvas_w_px - 2 * margin_px
    usable_h = canvas_h_px - 2 * margin_px
    if usable_w <= 0 or usable_h <= 0:
        raise TilerError("Margin leaves no room on the canvas — reduce it.")

    motif_px = max(8, int(round(usable_w / max(1, int(columns)))))
    motif_placements = _place_once(images, motif_px, motif_px)
    motif = _composite(images, motif_placements, motif_px, motif_px, None, design_scale=design_scale)

    cols_needed = math.ceil(usable_w / motif_px) if usable_w > 0 else 0
    rows_needed = math.ceil(usable_h / motif_px) if usable_h > 0 else 0

    canvas = Image.new("RGBA", (canvas_w_px, canvas_h_px), background or (0, 0, 0, 0))
    for r in range(rows_needed):
        for c in range(cols_needed):
            canvas.paste(motif, (margin_px + c * motif_px, margin_px + r * motif_px), motif)
    return canvas, cols_needed, rows_needed, len(images) * cols_needed * rows_needed


# ---------------------------------------------------------------------------
# Compositing
# ---------------------------------------------------------------------------

def _fit_within(img, w_px, h_px, scale=1.0, rotation_deg=0.0):
    """Resize `img` to fit within a w_px×h_px box, preserving aspect ratio,
    then apply an additional relative `scale` on top of that fit. `scale` is
    clamped to (0, 1] so the result can never exceed its box — the caller
    relies on that to guarantee placements never overlap. If `rotation_deg`
    is given, the image is rotated first (expanding its bounding box) and
    *that* rotated box is what gets fit — so a rotated tile still lands
    fully inside its cell."""
    from PIL import Image

    if img.width == 0 or img.height == 0:
        raise TilerError("A design image has zero size.")
    if rotation_deg:
        img = img.rotate(rotation_deg, expand=True, resample=Image.BICUBIC)
    scale = min(max(scale, 0.01), 1.0)
    fit = min(w_px / img.width, h_px / img.height) * scale
    new_w = max(1, int(round(img.width * fit)))
    new_h = max(1, int(round(img.height * fit)))
    return img.resize((new_w, new_h), Image.LANCZOS)


def _composite(images, placements, canvas_w_px, canvas_h_px, background, *,
                design_scale=1.0, jitter=0.0, rotation_jitter_deg=0.0, seed=0):
    """jitter and rotation_jitter_deg give the grid an organic "scattered"
    feel without any real collision risk: a tile is fit-to-cell (optionally
    rotated) first, which can only shrink it, and any positional jitter is
    then bounded by the leftover slack inside that same cell — so a tile
    can wiggle and tilt within its cell but never spill into a neighbor's."""
    import random
    from PIL import Image

    rng = random.Random(seed)
    canvas = Image.new("RGBA", (canvas_w_px, canvas_h_px), background or (0, 0, 0, 0))
    for p in placements:
        spec = images[p["index"]]
        x, y, w, h = p["cell"]
        rot = 0.0
        if rotation_jitter_deg and not spec.get("lock_rotation"):
            rot = rng.uniform(-rotation_jitter_deg, rotation_jitter_deg)
        tile = _fit_within(spec["image"], w, h, scale=spec["scale"] * design_scale, rotation_deg=rot)
        dx = x + (w - tile.width) / 2
        dy = y + (h - tile.height) / 2
        if jitter:
            slack_x = max(0.0, (w - tile.width) / 2)
            slack_y = max(0.0, (h - tile.height) / 2)
            dx += rng.uniform(-slack_x, slack_x) * jitter
            dy += rng.uniform(-slack_y, slack_y) * jitter
        canvas.paste(tile, (int(round(dx)), int(round(dy))), tile)
    return canvas


# ---------------------------------------------------------------------------
# Public conversion API
# ---------------------------------------------------------------------------

def convert(
    images,
    *,
    layout_mode="grid",
    preset=None,
    width_in=None,
    height_in=None,
    dpi=None,
    bleed_in=None,
    margin_in=DEFAULT_MARGIN_IN,
    spacing_in=DEFAULT_SPACING_IN,
    columns=DEFAULT_COLUMNS,
    design_scale=1.0,
    jitter=0.0,
    rotation_jitter_deg=0.0,
    background=None,
    seed=0,
    make_preview=True,
):
    """Arrange `images` onto a print canvas.

    images: a list of sources (path or file-like, e.g. io.BytesIO around
        uploaded bytes), or a list of dicts {"source", "name"?, "scale"?,
        "lock_rotation"?}. Per-image "scale" is a relative-size multiplier
        on top of the global design_scale (still capped so it can never
        exceed its cell); "lock_rotation" exempts that one design from
        rotation_jitter_deg — useful for text/logos that shouldn't tilt or
        flip while other, more decorative designs scatter freely.
    preset: a key into core.presets.PRODUCT_PRESETS, or None if width_in/
        height_in are given directly. Any of width_in/height_in/dpi/bleed_in
        passed explicitly overrides that field of the chosen preset.
    design_scale: how much of each grid cell a design fills, from just
        above 0 up to 1.0 (its cell, edge-to-edge — the default). Values
        below 1 leave visible air around every design.
    jitter: 0..1, how far a design may drift off-center within its cell
        (0 = perfectly centered grid, 1 = uses the full slack left by
        design_scale/spacing). Purely cosmetic — see _composite().
    rotation_jitter_deg: max random rotation (±) applied per design, in
        degrees. 0 = no rotation.
    background: None for transparent, or an (r, g, b) / (r, g, b, a) tuple.

    Returns:
        {
          "image_png":   bytes,        # full-resolution composited canvas
          "preview_png": bytes | None, # downscaled for on-screen display
          "stats": {
              "layout_mode", "image_count", "placed_count",
              "canvas_width_in", "canvas_height_in", "dpi", "bleed_in",
              "canvas_width_px", "canvas_height_px",
              "columns", "rows",   # grid: grid size. seamless: motif repeat
                                    # count. scatter: None (no fixed grid).
              "design_scale", "jitter", "rotation_jitter_deg",
          },
        }

    Raises TilerError for anything the end user should see.
    """
    design_scale = min(max(float(design_scale), 0.05), 1.0)
    jitter = min(max(float(jitter), 0.0), 1.0)
    rotation_jitter_deg = min(max(float(rotation_jitter_deg), 0.0), 180.0)
    if layout_mode not in LAYOUT_MODES:
        raise TilerError(
            f"Unknown layout mode '{layout_mode}'. Choose from: {', '.join(LAYOUT_MODES)}."
        )

    try:
        canvas_spec = resolve_canvas(preset, width_in=width_in, height_in=height_in,
                                      dpi=dpi, bleed_in=bleed_in)
    except KeyError:
        raise TilerError(
            f"Unknown preset '{preset}'. Choose from: {', '.join(sorted(PRODUCT_PRESETS))}."
        )
    except ValueError as exc:
        raise TilerError(str(exc)) from exc

    specs = _normalize_images(images)

    full_w_in = canvas_spec["width_in"] + 2 * canvas_spec["bleed_in"]
    full_h_in = canvas_spec["height_in"] + 2 * canvas_spec["bleed_in"]
    dpi_val = canvas_spec["dpi"]
    canvas_w_px = in_to_px(full_w_in, dpi_val)
    canvas_h_px = in_to_px(full_h_in, dpi_val)

    if canvas_w_px * canvas_h_px > MAX_CANVAS_PIXELS:
        raise TilerError(
            f"That canvas is {canvas_w_px}×{canvas_h_px}px at {dpi_val} DPI — too large to "
            "render safely in the browser. Lower the DPI or the canvas size."
        )

    margin_px = in_to_px(margin_in, dpi_val)
    spacing_px = in_to_px(spacing_in, dpi_val)

    columns_used = rows_used = None
    if layout_mode == "grid":
        placements, columns_used, rows_used = _layout_grid(
            specs, canvas_w_px, canvas_h_px,
            margin_px=margin_px, spacing_px=spacing_px, columns=columns,
        )
        log(f"Compositing {len(placements)} placement(s) of {len(specs)} design(s) onto "
            f"{canvas_w_px}x{canvas_h_px}px canvas (grid)...")
        canvas = _composite(
            specs, placements, canvas_w_px, canvas_h_px, background,
            design_scale=design_scale, jitter=jitter, rotation_jitter_deg=rotation_jitter_deg, seed=seed,
        )
        placed_count = len(placements)
    elif layout_mode == "scatter":
        placements, columns_used, rows_used = _layout_scatter(
            specs, canvas_w_px, canvas_h_px,
            margin_px=margin_px, spacing_px=spacing_px, columns=columns,
            design_scale=design_scale, jitter=jitter, seed=seed,
        )
        log(f"Compositing {len(placements)} placement(s) of {len(specs)} design(s) onto "
            f"{canvas_w_px}x{canvas_h_px}px canvas (scatter)...")
        # design_scale=1.0 here: _layout_scatter already sized the reserved
        # boxes by design_scale (see its docstring) — applying it again would
        # shrink everything twice.
        canvas = _composite(
            specs, placements, canvas_w_px, canvas_h_px, background,
            design_scale=1.0, jitter=jitter, rotation_jitter_deg=rotation_jitter_deg, seed=seed,
        )
        placed_count = len(placements)
    else:  # seamless
        canvas, columns_used, rows_used, placed_count = _layout_seamless(
            specs, canvas_w_px, canvas_h_px,
            margin_px=margin_px, columns=columns, design_scale=design_scale, background=background,
        )
        log(f"Tiled a {len(specs)}-design motif {columns_used}x{rows_used} times onto "
            f"{canvas_w_px}x{canvas_h_px}px canvas (seamless)...")

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    image_png = buf.getvalue()

    preview_png = None
    if make_preview:
        preview = canvas.copy()
        preview.thumbnail((PREVIEW_MAX_PX, PREVIEW_MAX_PX))
        pbuf = io.BytesIO()
        preview.save(pbuf, format="PNG")
        preview_png = pbuf.getvalue()

    stats = {
        "layout_mode": layout_mode,
        "image_count": len(specs),
        "placed_count": placed_count,
        "canvas_width_in": round(canvas_spec["width_in"], 3),
        "canvas_height_in": round(canvas_spec["height_in"], 3),
        "dpi": dpi_val,
        "bleed_in": round(canvas_spec["bleed_in"], 3),
        "canvas_width_px": canvas_w_px,
        "canvas_height_px": canvas_h_px,
        "columns": columns_used,
        "rows": rows_used,
        "design_scale": round(design_scale, 3),
        "jitter": round(jitter, 3),
        "rotation_jitter_deg": round(rotation_jitter_deg, 2),
    }
    log(f"Done: {stats['canvas_width_px']}x{stats['canvas_height_px']}px, {stats['placed_count']} placement(s)")
    return {"image_png": image_png, "preview_png": preview_png, "stats": stats}


# ---------------------------------------------------------------------------
# Main (thin CLI wrapper around convert())
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", help="one or more design image files")
    ap.add_argument("--out", default="pattern.png", help="output PNG filename")
    ap.add_argument("--layout", choices=LAYOUT_MODES, default="grid")
    ap.add_argument("--preset", choices=sorted(PRODUCT_PRESETS), default=None,
                     help="POD product preset (overridden by --width-in/--height-in if given)")
    ap.add_argument("--width-in", type=float, default=None)
    ap.add_argument("--height-in", type=float, default=None)
    ap.add_argument("--dpi", type=int, default=None)
    ap.add_argument("--bleed-in", type=float, default=None)
    ap.add_argument("--margin-in", type=float, default=DEFAULT_MARGIN_IN)
    ap.add_argument("--spacing-in", type=float, default=DEFAULT_SPACING_IN)
    ap.add_argument("--columns", type=int, default=DEFAULT_COLUMNS, help="grid mode: columns across the canvas")
    ap.add_argument("--design-scale", type=float, default=1.0,
                     help="fraction of each cell a design fills, 0-1 (default: 1.0, edge-to-edge)")
    ap.add_argument("--jitter", type=float, default=0.0,
                     help="0-1: how far designs may drift off-center within their cell, for a scattered feel")
    ap.add_argument("--rotation-jitter", type=float, default=0.0, dest="rotation_jitter",
                     help="max random rotation in degrees applied per design")
    ap.add_argument("--seed", type=int, default=0, help="random seed for --jitter/--rotation-jitter")
    args = ap.parse_args()

    if not args.preset and (args.width_in is None or args.height_in is None):
        ap.error("Pass --preset, or both --width-in and --height-in.")

    try:
        result = convert(
            args.images,
            layout_mode=args.layout,
            preset=args.preset,
            width_in=args.width_in,
            height_in=args.height_in,
            dpi=args.dpi,
            bleed_in=args.bleed_in,
            margin_in=args.margin_in,
            spacing_in=args.spacing_in,
            columns=args.columns,
            design_scale=args.design_scale,
            jitter=args.jitter,
            rotation_jitter_deg=args.rotation_jitter,
            seed=args.seed,
            make_preview=False,
        )
    except TilerError as exc:
        log(f"error: {exc}")
        sys.exit(1)

    with open(args.out, "wb") as fh:
        fh.write(result["image_png"])
    log(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
