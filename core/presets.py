"""Print-on-demand canvas presets: finished size, print DPI and bleed.

Sizes are sane defaults for each product, not universal truths — every POD
supplier's template differs slightly, so every field here can be overridden
per-job. `fabric_yard` models a continuous seamless-repeat unit rather than a
bordered product, so it carries no bleed.
"""

PRODUCT_PRESETS = {
    "mug_wrap": {
        "label": "Mug wrap (11oz, 9×3.5\")",
        "width_in": 9.0,
        "height_in": 3.5,
        "dpi": 300,
        "bleed_in": 0.125,
    },
    "tote_bag": {
        "label": "Tote bag (15×16\")",
        "width_in": 15.0,
        "height_in": 16.0,
        "dpi": 300,
        "bleed_in": 0.125,
    },
    "phone_case": {
        "label": "Phone case (3×6\")",
        "width_in": 3.0,
        "height_in": 6.0,
        "dpi": 300,
        "bleed_in": 0.0625,
    },
    "fabric_yard": {
        "label": "Fabric yard (continuous repeat, 36×36\")",
        "width_in": 36.0,
        "height_in": 36.0,
        "dpi": 150,
        "bleed_in": 0.0,
    },
    "tshirt_front": {
        "label": "T-shirt front (12×16\")",
        "width_in": 12.0,
        "height_in": 16.0,
        "dpi": 300,
        "bleed_in": 0.0,
    },
    "sticker_sheet": {
        "label": "Sticker sheet (8.5×11\")",
        "width_in": 8.5,
        "height_in": 11.0,
        "dpi": 300,
        "bleed_in": 0.125,
    },
}

DEFAULT_PRESET = "sticker_sheet"

# Safety cap on total canvas pixels. Pyodide runs in a WASM heap with a hard
# memory ceiling; an uncapped combination (e.g. a large custom size at high
# DPI) can OOM the tab instead of failing cleanly. ~40 megapixels covers every
# built-in preset at its default DPI with headroom.
MAX_CANVAS_PIXELS = 40_000_000


def resolve_canvas(preset=None, *, width_in=None, height_in=None, dpi=None, bleed_in=None):
    """Merge a named preset with explicit overrides into one canvas spec.
    Either `preset` or both of `width_in`/`height_in` must be given; any
    field also present is an override of that preset's default."""
    if preset is not None and preset not in PRODUCT_PRESETS:
        raise KeyError(preset)
    base = dict(PRODUCT_PRESETS[preset]) if preset else {}
    if width_in is None:
        width_in = base.get("width_in")
    if height_in is None:
        height_in = base.get("height_in")
    if width_in is None or height_in is None:
        raise ValueError("Pick a preset or supply width_in and height_in.")
    return {
        "width_in": float(width_in),
        "height_in": float(height_in),
        "dpi": int(dpi if dpi is not None else base.get("dpi", 300)),
        "bleed_in": float(bleed_in if bleed_in is not None else base.get("bleed_in", 0.0)),
    }
