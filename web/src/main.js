import './style.css'
// ?worker&url bundles the worker AND exposes its hashed URL, so the SW
// registration below can precache it (it's referenced only from JS, never HTML).
import workerUrl from './worker.js?worker&url'

// Keep in sync with CACHE in public/sw.js.
const SW_CACHE = 'stitchwright-v1-pyodide-314.0.6'

if ('serviceWorker' in navigator) {
  window.addEventListener('load', async () => {
    try {
      await navigator.serviceWorker.register(`${import.meta.env.BASE_URL}sw.js`)
    } catch {
      return // caching is an optimization; ignore failures
    }
    // On a cold first load the SW isn't controlling yet, so this document's
    // hashed shell assets are fetched without it. Their names are only known
    // here — precache them so an offline reload can boot.
    try {
      const cache = await caches.open(SW_CACHE)
      const urls = [
        ...[...document.querySelectorAll('script[src], link[rel="stylesheet"][href]')].map(
          (el) => el.src || el.href,
        ),
        new URL(workerUrl, location.href).href, // the module worker chunk
      ].filter((u) => u && u.startsWith(location.origin))
      if (urls.length) await cache.addAll([...new Set(urls)])
    } catch {
      /* private mode, storage blocked, offline — fine, cache-first fills later */
    }
  })
}

const $ = (id) => document.getElementById(id)

const els = {
  status: $('status'),
  spinner: $('spinner'),
  engineHint: $('engine-hint'),
  logbox: $('logbox'),
  log: $('log'),
  drop: $('drop'),
  file: $('file'),
  dropLabel: $('drop-label'),
  sourceThumb: $('source-thumb'),
  width: $('width'),
  unit: $('unit'),
  hoop: $('hoop'),
  advanced: $('advanced'),
  stitchType: $('stitch-type'),
  stitchLen: $('stitch-len'),
  satinStep: $('satin-step'),
  maxColors: $('max-colors'),
  minSpur: $('min-spur'),
  lock: $('lock'),
  formats: $('formats'),
  error: $('error'),
  go: $('go'),
  results: $('results'),
  hoopWarn: $('hoop-warn'),
  previewFigure: $('preview-figure'),
  preview: $('preview'),
  bgToggle: $('bg-toggle'),
  stats: $('stats'),
  downloads: $('downloads'),
}

let engineReady = false
let selectedFile = null
const objectUrls = new Set()

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------

const worker = new Worker(workerUrl, { type: 'module' })

worker.onmessage = (event) => {
  const msg = event.data
  switch (msg.type) {
    case 'status':
      els.status.textContent = msg.message
      break
    case 'log':
      appendLog(msg.line)
      break
    case 'ready':
      engineReady = true
      els.spinner.classList.add('done')
      els.status.textContent = 'Ready to convert.'
      els.engineHint.textContent = 'Ready and saved on your device. Choose an image below.'
      refreshGoButton()
      break
    case 'result':
      renderResult(msg)
      els.status.textContent = `Done — ${msg.stats.stitch_count.toLocaleString()} stitches.`
      setBusy(false)
      break
    case 'error':
      els.status.textContent = 'Error'
      appendLog(`ERROR: ${msg.message}`)
      showError(msg.message)
      setBusy(false)
      break
  }
}

function appendLog(line) {
  els.logbox.hidden = false
  els.log.textContent += line + '\n'
  els.log.scrollTop = els.log.scrollHeight
}

// ---------------------------------------------------------------------------
// File selection
// ---------------------------------------------------------------------------

els.file.addEventListener('change', () => {
  if (els.file.files && els.file.files[0]) setFile(els.file.files[0])
})

els.drop.addEventListener('dragover', (e) => {
  e.preventDefault()
  els.drop.classList.add('over')
})
els.drop.addEventListener('dragleave', () => els.drop.classList.remove('over'))
els.drop.addEventListener('drop', (e) => {
  e.preventDefault()
  els.drop.classList.remove('over')
  const f = e.dataTransfer.files && e.dataTransfer.files[0]
  if (f) setFile(f)
})
els.drop.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    els.file.click()
  }
})

function isSvg(file) {
  return file.type === 'image/svg+xml' || /\.svg$/i.test(file.name)
}

function setFile(file) {
  if (!file.type.startsWith('image/') && !isSvg(file)) {
    showError('Please choose an image file (PNG, JPG, WebP or SVG).')
    return
  }
  clearError()
  selectedFile = file
  const url = makeUrl(file)
  els.sourceThumb.src = url
  els.sourceThumb.hidden = false
  els.dropLabel.innerHTML = `<strong>${escapeHtml(file.name)}</strong><span>click to choose a different image</span>`
  refreshGoButton()
}

// ---------------------------------------------------------------------------
// Preview background (white by default; toggle to black for light-colored
// thread). Remembered per browser.
// ---------------------------------------------------------------------------

const BG_KEY = 'stitchwright-preview-bg'

function applyPreviewBg(mode) {
  const dark = mode === 'dark'
  els.preview.classList.toggle('on-dark', dark)
  els.bgToggle.textContent = dark ? 'Light background' : 'Dark background'
  els.bgToggle.setAttribute('aria-pressed', String(dark))
}

let previewBg = 'light'
try {
  if (localStorage.getItem(BG_KEY) === 'dark') previewBg = 'dark'
} catch {
  /* storage blocked — default to light */
}
applyPreviewBg(previewBg)

els.bgToggle.addEventListener('click', () => {
  previewBg = previewBg === 'dark' ? 'light' : 'dark'
  applyPreviewBg(previewBg)
  try {
    localStorage.setItem(BG_KEY, previewBg)
  } catch {
    /* ignore */
  }
})

// ---------------------------------------------------------------------------
// Convert
// ---------------------------------------------------------------------------

els.go.addEventListener('click', runConvert)

function selectedFormats() {
  return [...els.formats.querySelectorAll('input:checked')].map((i) => i.value)
}

function refreshGoButton() {
  els.go.disabled = !(engineReady && selectedFile && selectedFormats().length > 0)
}

els.formats.addEventListener('change', refreshGoButton)

function widthMm() {
  const raw = parseFloat(els.width.value)
  if (!isFinite(raw) || raw <= 0) return null
  return els.unit.value === 'in' ? raw * 25.4 : raw
}

async function runConvert() {
  const width_mm = widthMm()
  if (width_mm === null) {
    showError('Enter a finished width greater than zero.')
    return
  }
  const formats = selectedFormats()
  if (!selectedFile || formats.length === 0) return

  setBusy(true)
  els.log.textContent = ''
  clearError()

  const options = {
    width_mm,
    stitch_type: els.stitchType.value,
    stitch_len_mm: clampNum(els.stitchLen.value, 0.5, 8, 2.0),
    satin_step_mm: clampNum(els.satinStep.value, 0.15, 1.5, 0.4),
    max_colors: clampInt(els.maxColors.value, 1, 8, 4),
    min_spur_len: clampInt(els.minSpur.value, 1, 60, 15),
    lock_stitches: els.lock.checked,
    output_formats: formats,
    make_preview: true,
  }

  let imageBytes
  try {
    imageBytes = isSvg(selectedFile)
      ? await rasterizeSvg(selectedFile)
      : await selectedFile.arrayBuffer()
  } catch (err) {
    showError(`Could not read that image: ${err.message || err}`)
    setBusy(false)
    return
  }
  worker.postMessage({ type: 'convert', payload: { imageBytes, options } }, [imageBytes])
}

// SVG can't be decoded by Pillow in the worker, so rasterize it here with the
// browser's own renderer, then hand the worker a PNG.
const RASTER_MAX_PX = 1400

function rasterizeSvg(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      let w = img.naturalWidth
      let h = img.naturalHeight
      if (!w || !h) {
        w = h = 1024 // SVG with no intrinsic size
      }
      const scale = Math.min(1, RASTER_MAX_PX / Math.max(w, h))
      const canvas = document.createElement('canvas')
      canvas.width = Math.max(1, Math.round(w * scale))
      canvas.height = Math.max(1, Math.round(h * scale))
      const ctx = canvas.getContext('2d')
      ctx.fillStyle = '#ffffff' // flatten transparency onto the white the tool assumes
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
      URL.revokeObjectURL(url)
      canvas.toBlob((blob) => {
        if (!blob) return reject(new Error('rasterization failed'))
        blob.arrayBuffer().then(resolve, reject)
      }, 'image/png')
    }
    img.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error('the SVG could not be rendered'))
    }
    img.src = url
  })
}

function setBusy(busy) {
  els.go.disabled = busy || !engineReady || !selectedFile
  els.go.textContent = busy ? 'Working…' : 'Convert'
  els.spinner.classList.toggle('done', !busy && engineReady)
  if (busy) els.results.hidden = true
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

function renderResult({ formats, previewSvg, stats }) {
  revokeUrls()

  if (previewSvg) {
    els.preview.innerHTML = sanitizeSvg(previewSvg)
    els.previewFigure.hidden = false
  } else {
    els.preview.innerHTML = ''
    els.previewFigure.hidden = true
  }

  const mmToIn = (v) => (v / 25.4).toFixed(2)
  const swatches = (stats.colors || [])
    .map((c) => `<span class="swatch" style="background:rgb(${c[0]},${c[1]},${c[2]})" title="rgb(${c.join(', ')})"></span>`)
    .join('')

  els.stats.innerHTML =
    row('Stitches', stats.stitch_count.toLocaleString()) +
    row('Thread colors', `${stats.color_count} ${swatches}`) +
    row('Trims', stats.trim_count, 'times the thread is cut between sections') +
    row('Jumps', stats.jump_count, 'times the needle moves without stitching') +
    row('Stitched sections', stats.run_count, 'parts sewn in one continuous pass') +
    row(
      'Finished size',
      `${stats.width_mm} &times; ${stats.height_mm} mm <span class="muted">(${mmToIn(stats.width_mm)} &times; ${mmToIn(stats.height_mm)} in)</span>`,
    )

  const base = (selectedFile?.name || 'design').replace(/\.[^.]+$/, '') || 'design'
  els.downloads.innerHTML = ''
  for (const [ext, bytes] of Object.entries(formats)) {
    const blob = new Blob([bytes], { type: 'application/octet-stream' })
    const a = document.createElement('a')
    a.href = makeUrl(blob)
    a.download = `${base}.${ext}`
    a.className = 'download'
    a.innerHTML = `<span>${ext.toUpperCase()}</span><small>${formatKb(bytes.length)}</small>`
    els.downloads.appendChild(a)
  }

  checkHoop(stats)
  els.results.hidden = false
  els.results.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function row(label, value, note) {
  const n = note ? `<span class="stat-note">${note}</span>` : ''
  return `<div><dt>${label}${n}</dt><dd>${value}</dd></div>`
}

// pyembroidery's SVG is trusted output, but the markup lands via innerHTML, so
// strip anything scriptable before injecting it.
function sanitizeSvg(svg) {
  return svg
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '')
    .replace(/(href|xlink:href)\s*=\s*(["'])\s*javascript:[^"']*\2/gi, '')
}

function checkHoop(stats) {
  const sel = els.hoop.value
  if (!sel) {
    els.hoopWarn.hidden = true
    return
  }
  const [hw, hh] = sel.split('x').map(Number)
  const fits =
    (stats.width_mm <= hw && stats.height_mm <= hh) ||
    (stats.width_mm <= hh && stats.height_mm <= hw)
  if (fits) {
    els.hoopWarn.hidden = true
  } else {
    els.hoopWarn.hidden = false
    els.hoopWarn.textContent =
      `Heads up: ${stats.width_mm} × ${stats.height_mm} mm is larger than the selected ${hw} × ${hh} mm hoop. Reduce the finished width.`
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeUrl(blob) {
  const url = URL.createObjectURL(blob)
  objectUrls.add(url)
  return url
}

function revokeUrls() {
  // Keep the current source thumbnail; drop the rest before a re-render.
  for (const url of objectUrls) {
    if (url === els.sourceThumb.src) continue
    URL.revokeObjectURL(url)
    objectUrls.delete(url)
  }
}

function showError(message) {
  els.error.hidden = false
  els.error.textContent = message
  els.error.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function clearError() {
  els.error.hidden = true
  els.error.textContent = ''
}

function clampNum(v, lo, hi, dflt) {
  const n = parseFloat(v)
  if (!isFinite(n)) return dflt
  return Math.min(hi, Math.max(lo, n))
}

function clampInt(v, lo, hi, dflt) {
  const n = parseInt(v, 10)
  if (!Number.isFinite(n)) return dflt
  return Math.min(hi, Math.max(lo, n))
}

function formatKb(bytes) {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c])
}

window.addEventListener('beforeunload', () => {
  for (const url of objectUrls) URL.revokeObjectURL(url)
})
