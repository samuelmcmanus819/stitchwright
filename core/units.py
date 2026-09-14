"""Print-canvas math: inches/mm/px conversions at a given DPI. Shared so the
embroidery converter and the pattern tiler agree on units."""

MM_PER_IN = 25.4


def in_to_mm(inches):
    return inches * MM_PER_IN


def mm_to_in(mm):
    return mm / MM_PER_IN


def in_to_px(inches, dpi):
    return int(round(inches * dpi))


def px_to_in(px, dpi):
    return px / dpi
