# StitchWright

Turn a simple line drawing into a file your embroidery machine can stitch —
VP3, DST, PES, JEF, EXP, HUS or PEC.

**[Open the web app →](https://samuelmcmanus819.github.io/stitchwright/)**
It runs the converter entirely in your browser. Nothing is uploaded.

## What it's good for

Thin line art with a handful of flat colors: script text, monograms,
"made with love"–style lettering, outline hearts, logos, sketches. It
skeletonizes each color region to its centerline and stitches along it, as a
running stitch (a single thin line) or a satin stitch (a filled column).

## What it's *not* for

Filled/solid shapes, gradients, photos. Those need real digitizing software
such as [Ink/Stitch](https://inkstitch.org/) or SewArt. Auto-digitizing is
always approximate — **test-stitch on scrap fabric before a final piece.**

## Two ways to use it

### 1. Web app (no install)

<https://samuelmcmanus819.github.io/stitchwright/> — drop in a PNG, JPG, WebP or
SVG, pick a width and formats, and download. The first visit pulls the Python
runtime (~30 MB) and caches it, so later visits are instant and work offline.
See [`web/README.md`](web/README.md) to run or build it locally.

### 2. Command line

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

### As a library

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

## How it works

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

## Layout

| Path | What |
| --- | --- |
| `png_to_embroidery.py` | the converter — `convert()` API + CLI, one source of truth |
| `web/` | the browser app (Vite + Pyodide) that imports it verbatim |
| `.github/workflows/deploy.yml` | builds `web/` and deploys to GitHub Pages on push to `main` |

## License

No license yet — all rights reserved until one is added.
