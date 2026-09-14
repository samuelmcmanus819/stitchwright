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
  tabEmbroidery: $('tab-embroidery'),
  tabTiler: $('tab-tiler'),
  modeEmbroidery: $('mode-embroidery'),
  modeTiler: $('mode-tiler'),
  tilerDrop: $('tiler-drop'),
  tilerFile: $('tiler-file'),
  tilerList: $('tiler-list'),
  tilerPreset: $('tiler-preset'),
  tilerCustomFields: $('tiler-custom-fields'),
  tilerWidth: $('tiler-width'),
  tilerHeight: $('tiler-height'),
  tilerDpi: $('tiler-dpi'),
  tilerBleed: $('tiler-bleed'),
  tilerLayoutHelp: $('tiler-layout-help'),
  tilerArrangementField: $('tiler-arrangement-field'),
  tilerArrangement: $('tiler-arrangement'),
  tilerSize: $('tiler-size'),
  tilerSizeOut: $('tiler-size-out'),
  tilerDensity: $('tiler-density'),
  tilerDensityOut: $('tiler-density-out'),
  tilerDensityHelp: $('tiler-density-help'),
  tilerMargin: $('tiler-margin'),
  tilerSpacingField: $('tiler-spacing-field'),
  tilerSpacingHelp: $('tiler-spacing-help'),
  tilerSpacing: $('tiler-spacing'),
  tilerJitterField: $('tiler-jitter-field'),
  tilerJitterHelp: $('tiler-jitter-help'),
  tilerJitter: $('tiler-jitter'),
  tilerRotationField: $('tiler-rotation-field'),
  tilerRotationHelp: $('tiler-rotation-help'),
  tilerRotation: $('tiler-rotation'),
  tilerError: $('tiler-error'),
  tilerGo: $('tiler-go'),
  tilerShuffle: $('tiler-shuffle'),
  tilerResults: $('tiler-results'),
  tilerPreviewFigure: $('tiler-preview-figure'),
  tilerPreview: $('tiler-preview'),
  tilerStats: $('tiler-stats'),
  tilerDownloads: $('tiler-downloads'),
}

let engineReady = false
let selectedFile = null
let activeMode = 'embroidery' // routes worker responses to the right panel
const objectUrls = new Set()
const tilerFiles = [] // { file, url, name, scale, lockRotation }
let tilerSeed = 0 // bumped by the Shuffle button; stable across a plain Generate

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
      refreshTilerGoButton()
      break
    case 'result':
      renderResult(msg)
      els.status.textContent = `Done — ${msg.stats.stitch_count.toLocaleString()} stitches.`
      setBusy(false)
      break
    case 'tileResult':
      renderTilerResult(msg)
      els.status.textContent = `Done — ${msg.stats.placed_count.toLocaleString()} design(s) placed.`
      setTilerBusy(false)
      break
    case 'error':
      els.status.textContent = 'Error'
      appendLog(`ERROR: ${msg.message}`)
      if (activeMode === 'tiler') {
        showTilerError(msg.message)
        setTilerBusy(false)
      } else {
        showError(msg.message)
        setBusy(false)
      }
      break
  }
}

function appendLog(line) {
  els.logbox.hidden = false
  els.log.textContent += line + '\n'
  els.log.scrollTop = els.log.scrollHeight
}

// ---------------------------------------------------------------------------
// Mode tabs
// ---------------------------------------------------------------------------

function setMode(mode) {
  activeMode = mode
  const embroidery = mode === 'embroidery'
  els.modeEmbroidery.hidden = !embroidery
  els.modeTiler.hidden = embroidery
  els.tabEmbroidery.classList.toggle('active', embroidery)
  els.tabTiler.classList.toggle('active', !embroidery)
  els.tabEmbroidery.setAttribute('aria-selected', String(embroidery))
  els.tabTiler.setAttribute('aria-selected', String(!embroidery))
}

els.tabEmbroidery.addEventListener('click', () => setMode('embroidery'))
els.tabTiler.addEventListener('click', () => setMode('tiler'))

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
  activeMode = 'embroidery' // in case a tiler job is also in flight when this resolves
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
// Pattern Tiler
// ---------------------------------------------------------------------------

const tilerThumbUrls = new Set()
let tilerResultUrls = []

els.tilerFile.addEventListener('change', () => {
  if (els.tilerFile.files && els.tilerFile.files.length) addTilerFiles(els.tilerFile.files)
  els.tilerFile.value = '' // allow re-adding a removed file
})

els.tilerDrop.addEventListener('dragover', (e) => {
  e.preventDefault()
  els.tilerDrop.classList.add('over')
})
els.tilerDrop.addEventListener('dragleave', () => els.tilerDrop.classList.remove('over'))
els.tilerDrop.addEventListener('drop', (e) => {
  e.preventDefault()
  els.tilerDrop.classList.remove('over')
  if (e.dataTransfer.files && e.dataTransfer.files.length) addTilerFiles(e.dataTransfer.files)
})
els.tilerDrop.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    els.tilerFile.click()
  }
})

function addTilerFiles(fileList) {
  clearTilerError()
  for (const file of fileList) {
    if (!file.type.startsWith('image/') && !isSvg(file)) continue // silently skip a stray non-image
    const url = URL.createObjectURL(file) // browsers render SVG blobs in <img> directly, so thumbnails need no rasterization
    tilerThumbUrls.add(url)
    tilerFiles.push({ file, url, name: file.name, scale: 1, lockRotation: false })
  }
  renderTilerList()
  refreshTilerGoButton()
}

function renderTilerList() {
  els.tilerList.innerHTML = ''
  tilerFiles.forEach((item, i) => {
    const el = document.createElement('div')
    el.className = 'tile-item'
    el.innerHTML = `
      <img src="${item.url}" alt="${escapeHtml(item.name)}" />
      <span class="tile-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
      <div class="tile-controls">
        <input type="number" class="tile-scale" min="0.3" max="1.5" step="0.1" value="${item.scale}"
          title="Relative scale within its cell" />
        <label class="tile-lock" title="Keep this design from rotating">
          <input type="checkbox" ${item.lockRotation ? 'checked' : ''} /> &#128274;
        </label>
      </div>
      <button type="button">Remove</button>
    `
    el.querySelector('.tile-scale').addEventListener('change', (e) => {
      item.scale = clampNum(e.target.value, 0.3, 1.5, 1)
      e.target.value = item.scale
    })
    const lockLabel = el.querySelector('.tile-lock')
    lockLabel.querySelector('input').addEventListener('change', (e) => {
      item.lockRotation = e.target.checked
      lockLabel.classList.toggle('on', item.lockRotation)
    })
    el.querySelector('button').addEventListener('click', () => {
      URL.revokeObjectURL(item.url)
      tilerThumbUrls.delete(item.url)
      tilerFiles.splice(i, 1)
      renderTilerList()
      refreshTilerGoButton()
    })
    els.tilerList.appendChild(el)
  })
}

els.tilerPreset.addEventListener('change', () => {
  els.tilerCustomFields.hidden = els.tilerPreset.value !== 'custom'
})

// "Spacing style" presets (Grid repeat only): quick-set the underlying
// spacing/drift/tilt knobs. Still plain number inputs underneath, so a user
// can nudge any of them afterward under "More options" without losing the
// rest of the preset.
const ARRANGEMENTS = {
  even: { spacing_in: 0.125, jitter: 0, rotation: 0 },
  loose: { spacing_in: 0.375, jitter: 0, rotation: 0 },
  scattered: { spacing_in: 0.1875, jitter: 85, rotation: 15 },
}

function applyArrangement() {
  const a = ARRANGEMENTS[els.tilerArrangement.value] || ARRANGEMENTS.even
  els.tilerSpacing.value = a.spacing_in
  els.tilerJitter.value = a.jitter
  els.tilerRotation.value = a.rotation
}

els.tilerArrangement.addEventListener('change', applyArrangement)

// Layout mode changes what several controls mean, so copy and defaults are
// swapped in per mode rather than reusing one static set of labels.
const LAYOUT_HELP = {
  grid: '<strong>Grid repeat</strong> cycles your designs round-robin through a fixed grid of cells that fills the whole canvas — good for sticker sheets and alternating patterns.',
  scatter: '<strong>Scatter/random</strong> places designs at random positions, sizes and rotations, retrying each one to guarantee nothing overlaps — good for florals and organic sticker sheets.',
  seamless: '<strong>Seamless tile</strong> arranges your designs once into a small repeat unit, then tiles it edge-to-edge with no visible seam — good for continuous fabric-style patterns.',
}
const DENSITY_HELP = {
  grid: 'How many designs fit across the canvas. Rows fill in automatically.',
  scatter: 'Roughly how many designs to scatter across the canvas.',
  seamless: 'How many times the repeat unit tiles across the canvas width.',
}

function tilerLayoutMode() {
  const checked = document.querySelector('input[name="tiler-layout"]:checked')
  return checked ? checked.value : 'grid'
}

function updateLayoutModeUI() {
  const mode = tilerLayoutMode()
  els.tilerLayoutHelp.innerHTML = LAYOUT_HELP[mode]
  els.tilerDensityHelp.textContent = DENSITY_HELP[mode]

  els.tilerArrangementField.hidden = mode !== 'grid'
  els.tilerSpacingField.hidden = mode === 'seamless'
  els.tilerJitterField.hidden = mode === 'seamless'
  els.tilerRotationField.hidden = mode === 'seamless'

  if (mode === 'grid') {
    applyArrangement() // restore whatever "Spacing style" is currently selected
    els.tilerJitterHelp.textContent = 'How far off-center a design may land. Set by the spacing style above.'
    els.tilerRotationHelp.textContent = 'Maximum random rotation per design. Set by the spacing style above.'
  } else if (mode === 'scatter') {
    els.tilerJitter.value = 50
    els.tilerRotation.value = 180
    els.tilerJitterHelp.textContent = 'How much design sizes vary from one to the next.'
    els.tilerRotationHelp.textContent = 'Maximum random rotation per design.'
  }
}

document.querySelectorAll('input[name="tiler-layout"]').forEach((el) => {
  el.addEventListener('change', updateLayoutModeUI)
})
updateLayoutModeUI()

els.tilerSize.addEventListener('input', () => {
  els.tilerSizeOut.textContent = `${els.tilerSize.value}%`
})

els.tilerDensity.addEventListener('input', () => {
  els.tilerDensityOut.textContent = `${els.tilerDensity.value} across`
})

function refreshTilerGoButton() {
  const disabled = !(engineReady && tilerFiles.length > 0) || els.tilerGo.dataset.busy === '1'
  els.tilerGo.disabled = disabled
  els.tilerShuffle.disabled = disabled
}

els.tilerGo.addEventListener('click', () => runTiler())
els.tilerShuffle.addEventListener('click', () => {
  tilerSeed = Math.floor(Math.random() * 1_000_000)
  runTiler()
})

async function runTiler() {
  if (!tilerFiles.length) return
  activeMode = 'tiler' // in case an embroidery job is also in flight when this resolves
  clearTilerError()
  setTilerBusy(true)
  els.log.textContent = ''

  const options = {
    layout_mode: tilerLayoutMode(),
    columns: clampInt(els.tilerDensity.value, 2, 10, 4),
    margin_in: clampNum(els.tilerMargin.value, 0, 3, 0.125),
    spacing_in: clampNum(els.tilerSpacing.value, 0, 3, 0.125),
    design_scale: clampNum(els.tilerSize.value, 40, 100, 100) / 100,
    jitter: clampNum(els.tilerJitter.value, 0, 100, 0) / 100,
    rotation_jitter_deg: clampNum(els.tilerRotation.value, 0, 180, 0),
    seed: tilerSeed,
    make_preview: true,
  }
  if (els.tilerPreset.value === 'custom') {
    options.width_in = clampNum(els.tilerWidth.value, 0.5, 200, 8.5)
    options.height_in = clampNum(els.tilerHeight.value, 0.5, 200, 11)
    options.dpi = clampInt(els.tilerDpi.value, 72, 600, 300)
    options.bleed_in = clampNum(els.tilerBleed.value, 0, 3, 0.125)
  } else {
    options.preset = els.tilerPreset.value
  }

  let images
  try {
    images = await Promise.all(
      tilerFiles.map(async (item) => ({
        name: item.name,
        scale: item.scale,
        lockRotation: item.lockRotation,
        bytes: isSvg(item.file) ? await rasterizeSvg(item.file) : await item.file.arrayBuffer(),
      })),
    )
  } catch (err) {
    showTilerError(`Could not read a design image: ${err.message || err}`)
    setTilerBusy(false)
    return
  }

  worker.postMessage(
    { type: 'tile', payload: { images, options } },
    images.map((img) => img.bytes),
  )
}

function setTilerBusy(busy) {
  els.tilerGo.dataset.busy = busy ? '1' : '0'
  const disabled = busy || !engineReady || tilerFiles.length === 0
  els.tilerGo.disabled = disabled
  els.tilerShuffle.disabled = disabled
  els.tilerGo.textContent = busy ? 'Working…' : 'Generate layout'
  if (busy) els.tilerResults.hidden = true
}

function renderTilerResult({ imagePng, previewPng, stats }) {
  for (const url of tilerResultUrls) URL.revokeObjectURL(url)
  tilerResultUrls = []

  const makeResultUrl = (blob) => {
    const url = URL.createObjectURL(blob)
    tilerResultUrls.push(url)
    return url
  }

  const previewUrl = makeResultUrl(new Blob([previewPng || imagePng], { type: 'image/png' }))
  els.tilerPreview.innerHTML = `<img src="${previewUrl}" alt="Pattern layout preview" />`

  const gridRow = stats.columns
    ? row('Grid', `${stats.columns} &times; ${stats.rows}`)
    : ''
  els.tilerStats.innerHTML =
    row(
      'Canvas size',
      `${stats.canvas_width_in} &times; ${stats.canvas_height_in} in ` +
        `<span class="muted">(${stats.canvas_width_px}&times;${stats.canvas_height_px}px @ ${stats.dpi} DPI)</span>`,
    ) +
    row('Bleed', `${stats.bleed_in} in`) +
    row('Layout', stats.layout_mode) +
    row('Designs used', stats.image_count) +
    row('Placed', stats.placed_count) +
    gridRow +
    row('Design size', `${Math.round(stats.design_scale * 100)}%`) +
    (stats.layout_mode !== 'seamless' && stats.jitter > 0
      ? row('Drift / tilt', `${Math.round(stats.jitter * 100)}% / ±${stats.rotation_jitter_deg}°`)
      : '')

  const fullUrl = makeResultUrl(new Blob([imagePng], { type: 'image/png' }))
  els.tilerDownloads.innerHTML = ''
  const a = document.createElement('a')
  a.href = fullUrl
  a.download = 'pattern.png'
  a.className = 'download'
  a.innerHTML = `<span>PNG</span><small>${formatKb(imagePng.length)} &middot; full resolution</small>`
  els.tilerDownloads.appendChild(a)

  els.tilerResults.hidden = false
  els.tilerResults.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function showTilerError(message) {
  els.tilerError.hidden = false
  els.tilerError.textContent = message
  els.tilerError.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function clearTilerError() {
  els.tilerError.hidden = true
  els.tilerError.textContent = ''
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
  for (const url of tilerThumbUrls) URL.revokeObjectURL(url)
  for (const url of tilerResultUrls) URL.revokeObjectURL(url)
})
