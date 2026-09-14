# StitchWright

Two tools for turning artwork into production-ready files, entirely in your
browser — nothing is uploaded.

**[Open the web app →](https://samuelmcmanus819.github.io/stitchwright/)**

## Embroidery converter

Turn a simple line drawing into a file your embroidery machine can stitch —
VP3, DST, PES, JEF, EXP, HUS or PEC.

### What it's good for

Thin line art with a handful of flat colors: script text, monograms,
"made with love"–style lettering, outline hearts, logos, sketches. It
skeletonizes each color region to its centerline and stitches along it, as a
running stitch (a single thin line) or a satin stitch (a filled column).

### What it's *not* for

Filled/solid shapes, gradients, photos. Those need real digitizing software
such as [Ink/Stitch](https://inkstitch.org/) or SewArt. Auto-digitizing is
always approximate — **test-stitch on scrap fabric before a final piece.**

### Two ways to use it

**Web app (no install):**
<https://samuelmcmanus819.github.io/stitchwright/> — drop in a PNG, JPG, WebP or
SVG, pick a width and formats, and download. The first visit pulls the Python
runtime (~30 MB) and caches it, so later visits are instant and work offline.
See [`web/README.md`](web/README.md) to run or build it locally.

**Command line:**

```bash
pip install pyembroidery scikit-image networkx numpy pillow scipy

python png_to_embroidery.py input.png --out design \
    --width-mm 120 --stitch-type running --formats vp3,dst,pes
```

Writes `design.<fmt>` for each requested format plus `design_preview.svg`.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--out` | `design` | output filename prefix |
| `--width-mm` | `120` | finished design width in mm |
| `--stitch-type` | `running` | `running` (thin line) or `satin` (filled) |
| `--stitch-len-mm` | `2.0` | running-stitch spacing |
| `--satin-step-mm` | `0.4` | satin zigzag step |
| `--max-colors` | `4` | max thread colors to detect |
| `--min-spur-len` | `15` | prune skeleton spurs shorter than this (px) |
| `--formats` | `vp3,dst` | any of `vp3 dst pes jef exp hus pec` |
| `--no-lock-stitches` | off | disable tie-in/tie-off lock stitches |
| `--no-preview` | off | skip the SVG preview |

**As a library:**

```python
from png_to_embroidery import convert, ConversionError

result = convert(image_bytes_or_path, width_mm=120,
                 stitch_type="running", output_formats=["vp3", "dst"])
result["formats"]["vp3"]   # -> bytes
result["preview_svg"]      # -> SVG string
result["stats"]            # -> stitch_count, trim_count, colors, dimensions, ...
```

`convert()` never touches the filesystem, which is what lets it run unchanged in
the browser under Pyodide.

### How it works

1. Separate the ink pixels into thread colors (k-means), merging anti-aliasing
   halos back into their real color.
2. Skeletonize each color to 1px centerlines; prune tiny spurs; merge junction
   clusters into single nodes.
3. Decompose each connected skeleton into a small graph and route it with an
   Eulerian circuit — one continuous path that covers every stroke, retracing
   only where the topology forces it (so trims land roughly once per separate
   ink shape, not once per letter crossing).
4. Running stitch: resample the path to even spacing. Satin: take the local
   half-width from the mask's distance transform and zigzag between the two
   rails.
5. Add lock stitches at every jump/trim, order the runs with a nearest-neighbor
   pass, and emit the files with `pyembroidery`.

## Pattern Tiler

Arrange one or more design images onto a canvas sized for a specific
print-on-demand product — mug wrap, tote bag, phone case, T-shirt front,
sticker sheet, or a continuous fabric-yard repeat — instead of resizing and
repositioning each one by hand in Canva.

Pick your images (PNG, JPG, WebP or SVG), a product preset (or a custom
size/DPI/bleed), a layout mode, and download a full-resolution PNG. The
**Design size** and **Density** sliders control how big each design is and
how many fit across. Each design in the list also gets its own relative
**scale** and a **🔒 lock rotation** toggle (useful for text/logos that
shouldn't tilt while other designs scatter freely). Not happy with a random
result? **Shuffle** regenerates it with a new arrangement without touching
any other setting.

**Layout modes:**

| Mode | What |
| --- | --- |
| Grid repeat | Designs cycle round-robin through a fixed grid of cells that fills the whole canvas. The **Spacing style** preset (Even grid / Loose grid / Scattered) sets how much they drift off-center and tilt for a more organic look — still collision-free, since a design can only ever move within the slack left in its own cell. |
| Scatter/random | Designs at random positions, sizes and rotations, placed with a retry loop and a bounding-circle check so nothing overlaps. |
| Seamless tile | Every design arranged once into a small repeat unit (each fully inside its own cell), then tiled edge-to-edge across the canvas with no visible seam — the CSS `background-repeat` principle. |

**Command line:**

```bash
python pattern_tiler.py sticker1.png sticker2.png \
    --preset sticker_sheet --layout grid --out sheet.png
```

**As a library:**

```python
from pattern_tiler import convert, TilerError

result = convert(["a.png", "b.png"], preset="sticker_sheet")
result["image_png"]    # -> bytes, full-resolution composited PNG
result["preview_png"]  # -> bytes, downscaled for on-screen display
result["stats"]        # -> canvas size, DPI, placement counts, ...
```

Same shape as the embroidery converter's `convert()`: no filesystem access,
so it runs unchanged in the browser under Pyodide.

## Layout

| Path | What |
| --- | --- |
| `png_to_embroidery.py` | the embroidery converter — `convert()` API + CLI |
| `pattern_tiler.py` | the pattern tiler — `convert()` API + CLI |
| `core/` | image I/O, unit math and POD presets shared by both modules |
| `web/` | the browser app (Vite + Pyodide) that imports both verbatim |
| `.github/workflows/deploy.yml` | builds `web/` and deploys to GitHub Pages on push to `main` |

## License

No license yet — all rights reserved until one is added.
