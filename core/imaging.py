"""Image I/O helpers shared by the embroidery converter and the pattern
tiler. Everything here takes a filesystem path or a file-like object (e.g.
io.BytesIO around uploaded bytes) and never touches the filesystem itself —
that's what lets both callers run unchanged in a browser under Pyodide.
"""


def rewind(source):
    """Seek a file-like source back to 0 if it supports it. A caller may
    have already read/peeked at it (or this is the second time the same
    source is opened); a no-op for plain paths and for streams that don't
    support seeking."""
    if hasattr(source, "seek"):
        try:
            source.seek(0)
        except Exception:
            pass


def load_rgba(source):
    """Open an image source and return it as a PIL Image in RGBA mode."""
    from PIL import Image

    rewind(source)
    return Image.open(source).convert("RGBA")
