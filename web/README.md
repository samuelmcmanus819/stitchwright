# StitchWright web

A static, no-backend web front-end for `../png_to_embroidery.py`. The Python
converter runs in the browser via [Pyodide](https://pyodide.org/); nothing is
uploaded.

## Develop

```bash
cd web
npm install
npm run dev
```

Open the printed URL. The first load pulls Pyodide + numpy/scipy/scikit-image
(~30 MB) from the jsDelivr CDN and caches it (see `src/sw.js`).

## Build

```bash
npm run build      # -> web/dist/
npm run preview    # serve the built site locally
```

## How it fits together

| File | Role |
| --- | --- |
| `index.html` / `src/main.js` / `src/style.css` | UI: upload, presets, format picker, preview, downloads |
| `src/worker.js` | Web Worker: boots Pyodide, `micropip`-installs the vendored `pyembroidery` wheel, calls `convert()` |
| `../png_to_embroidery.py` | The actual algorithm, imported verbatim as text via `?raw` — one source of truth shared with the CLI |
| `public/vendor/pyembroidery-*.whl` | Vendored so a first run never depends on PyPI CORS |

`convert()` returns file bytes + a preview + stats and never touches a
filesystem, which is what lets it run unchanged under Pyodide.

## Updating

- **Algorithm change:** edit `../png_to_embroidery.py`. The CLI and the web app
  both pick it up; rebuild the site.
- **Pyodide version:** bump `PYODIDE_VERSION` in `src/worker.js` (and the
  `CACHE` name in `src/sw.js` so clients refetch). Check the new release's
  `pyodide-lock.json` still ships numpy, scipy, scikit-image, networkx and
  Pillow.
- **pyembroidery version:** drop the new wheel in `public/vendor/` and update the
  path in `src/worker.js`.

## Deploy

`.github/workflows/deploy.yml` builds this folder and publishes `web/dist/` to
GitHub Pages on every push to `main`. Enable Pages → "GitHub Actions" in the
repo settings once.
