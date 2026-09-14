// Runs entirely off the main thread: boots Pyodide, loads the scientific
// stack + pyembroidery, then calls png_to_embroidery.convert() (or
// pattern_tiler.convert()) per request. Both Python modules — plus the
// core/ package they share — are bundled in verbatim as text and written
// into Pyodide's virtual FS below; there's no server involved.
//
// Message protocol
//   main -> worker : { type: 'convert', payload: { imageBytes: ArrayBuffer, options } }
//                    { type: 'tile', payload: { images: [{ name, scale, lockRotation, bytes: ArrayBuffer }], options } }
//   worker -> main : { type: 'status', stage, message }   progress ticks
//                    { type: 'log', line }                Python stdout/stderr
//                    { type: 'ready' }                    engine warm, accepts convert/tile
//                    { type: 'result', formats, previewSvg, stats }        (convert)
//                    { type: 'tileResult', imagePng, previewPng, stats }   (tile)
//                    { type: 'error', message }

import pySource from '../../png_to_embroidery.py?raw'
import tilerSource from '../../pattern_tiler.py?raw'
import coreInitSource from '../../core/__init__.py?raw'
import coreErrorsSource from '../../core/errors.py?raw'
import coreImagingSource from '../../core/imaging.py?raw'
import coreUnitsSource from '../../core/units.py?raw'
import corePresetsSource from '../../core/presets.py?raw'
import pyembroideryWheelRel from '../vendor/pyembroidery-1.5.1-py2.py3-none-any.whl?url'

const PYODIDE_VERSION = '314.0.6'
const CDN = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`

// Vendored so a first run never depends on PyPI CORS. Vite hands us a path
// relative to this module's built location; resolve it to an absolute URL the
// worker can fetch in both dev and production.
const PYEMBROIDERY_WHEEL = new URL(pyembroideryWheelRel, import.meta.url).href

const GLUE = `
import sys, io, json
sys.path.insert(0, '/')
import png_to_embroidery as _pe
import pattern_tiler as _pt


def _to_bytesio(u8):
    buf = u8.to_py()
    try:
        return io.BytesIO(buf.tobytes())
    except AttributeError:
        return io.BytesIO(bytes(buf))


def _run(image_u8, options_json):
    opts = json.loads(options_json)
    try:
        res = _pe.convert(_to_bytesio(image_u8), **opts)
    except _pe.ConversionError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # last-resort: surface instead of a bare traceback
        return {"ok": False, "error": "Unexpected error: %s" % exc}
    return {
        "ok": True,
        "formats": res["formats"],
        "preview_svg": res["preview_svg"],
        "stats": res["stats"],
    }


def _run_tiler(images_bytes_list, images_meta_json, options_json):
    metas = json.loads(images_meta_json)
    opts = json.loads(options_json)
    images = [
        {
            "source": _to_bytesio(u8),
            "name": meta.get("name"),
            "scale": meta.get("scale", 1.0),
            "lock_rotation": meta.get("lock_rotation", False),
        }
        for u8, meta in zip(images_bytes_list, metas)
    ]
    try:
        res = _pt.convert(images, **opts)
    except _pt.TilerError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # last-resort: surface instead of a bare traceback
        return {"ok": False, "error": "Unexpected error: %s" % exc}
    return {
        "ok": True,
        "image_png": res["image_png"],
        "preview_png": res["preview_png"],
        "stats": res["stats"],
    }
`

const post = (msg) => self.postMessage(msg)

let runFn = null
let runTilerFn = null

async function init() {
  post({ type: 'status', stage: 'boot', message: 'Downloading the Python runtime…' })
  const { loadPyodide } = await import(/* @vite-ignore */ `${CDN}pyodide.mjs`)
  const pyodide = await loadPyodide({ indexURL: CDN })

  pyodide.setStdout({ batched: (line) => post({ type: 'log', line }) })
  pyodide.setStderr({ batched: (line) => post({ type: 'log', line }) })

  post({ type: 'status', stage: 'packages', message: 'Loading numpy, scipy, scikit-image…' })
  await pyodide.loadPackage([
    'numpy',
    'scipy',
    'scikit-image',
    'networkx',
    'Pillow',
    'micropip',
  ])

  post({ type: 'status', stage: 'pyembroidery', message: 'Installing pyembroidery…' })
  const micropip = pyodide.pyimport('micropip')
  await micropip.install(PYEMBROIDERY_WHEEL)

  post({ type: 'status', stage: 'glue', message: 'Warming up the converter…' })
  pyodide.FS.mkdirTree('/core')
  pyodide.FS.writeFile('/core/__init__.py', coreInitSource)
  pyodide.FS.writeFile('/core/errors.py', coreErrorsSource)
  pyodide.FS.writeFile('/core/imaging.py', coreImagingSource)
  pyodide.FS.writeFile('/core/units.py', coreUnitsSource)
  pyodide.FS.writeFile('/core/presets.py', corePresetsSource)
  pyodide.FS.writeFile('/png_to_embroidery.py', pySource)
  pyodide.FS.writeFile('/pattern_tiler.py', tilerSource)
  pyodide.runPython(GLUE)
  runFn = pyodide.globals.get('_run')
  runTilerFn = pyodide.globals.get('_run_tiler')

  post({ type: 'ready' })
}

const ready = init().catch((err) => {
  post({ type: 'error', message: `Startup failed: ${err && err.message ? err.message : err}` })
})

self.onmessage = async (event) => {
  const msg = event.data
  if (!msg) return

  await ready

  if (msg.type === 'convert') {
    if (!runFn) {
      post({ type: 'error', message: 'The stitching engine is still loading — try again in a moment.' })
      return
    }

    const { imageBytes, options } = msg.payload
    post({ type: 'status', stage: 'convert', message: 'Tracing centerlines and generating stitches…' })

    let resProxy = null
    try {
      const u8 = new Uint8Array(imageBytes)
      resProxy = runFn(u8, JSON.stringify(options))
      const out = resProxy.toJs({ dict_converter: Object.fromEntries })
      if (!out.ok) {
        post({ type: 'error', message: out.error })
        return
      }
      post({
        type: 'result',
        formats: out.formats, // { ext: Uint8Array }
        previewSvg: out.preview_svg, // SVG markup string | null
        stats: out.stats,
      })
    } catch (err) {
      post({ type: 'error', message: err && err.message ? err.message : String(err) })
    } finally {
      if (resProxy) resProxy.destroy()
    }
    return
  }

  if (msg.type === 'tile') {
    if (!runTilerFn) {
      post({ type: 'error', message: 'The layout engine is still loading — try again in a moment.' })
      return
    }

    const { images, options } = msg.payload
    post({ type: 'status', stage: 'tile', message: 'Laying out the pattern…' })

    let resProxy = null
    try {
      const bytesList = images.map((img) => new Uint8Array(img.bytes))
      const meta = images.map((img) => ({ name: img.name, scale: img.scale, lock_rotation: img.lockRotation }))
      resProxy = runTilerFn(bytesList, JSON.stringify(meta), JSON.stringify(options))
      const out = resProxy.toJs({ dict_converter: Object.fromEntries })
      if (!out.ok) {
        post({ type: 'error', message: out.error })
        return
      }
      post({
        type: 'tileResult',
        imagePng: out.image_png, // Uint8Array, full-resolution PNG
        previewPng: out.preview_png, // Uint8Array, downscaled for display
        stats: out.stats,
      })
    } catch (err) {
      post({ type: 'error', message: err && err.message ? err.message : String(err) })
    } finally {
      if (resProxy) resProxy.destroy()
    }
    return
  }
}
