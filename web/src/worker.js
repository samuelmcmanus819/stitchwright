// Runs entirely off the main thread: boots Pyodide, loads the scientific
// stack + pyembroidery, then calls png_to_embroidery.convert() per request.
//
// Message protocol
//   main -> worker : { type: 'convert', payload: { imageBytes: ArrayBuffer, options } }
//   worker -> main : { type: 'status', stage, message }   progress ticks
//                    { type: 'log', line }                Python stdout/stderr
//                    { type: 'ready' }                    engine warm, accepts convert
//                    { type: 'result', formats, previewSvg, stats }
//                    { type: 'error', message }

import pySource from '../../png_to_embroidery.py?raw'
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
`

const post = (msg) => self.postMessage(msg)

let runFn = null

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
  pyodide.FS.writeFile('/png_to_embroidery.py', pySource)
  pyodide.runPython(GLUE)
  runFn = pyodide.globals.get('_run')

  post({ type: 'ready' })
}

const ready = init().catch((err) => {
  post({ type: 'error', message: `Startup failed: ${err && err.message ? err.message : err}` })
})

self.onmessage = async (event) => {
  const msg = event.data
  if (!msg || msg.type !== 'convert') return

  await ready
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
}
