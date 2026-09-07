import { defineConfig } from 'vite'

// The Python converter (../png_to_embroidery.py) is shared verbatim with the
// CLI. worker.js pulls it in as a string via `?raw`, so Vite needs read
// access one level above web/. `base: './'` keeps asset URLs relative so the
// build works from a GitHub Pages project subpath.
export default defineConfig({
  base: './',
  // The vendored pyembroidery wheel is imported with ?url so Vite fingerprints
  // it and rewrites the path for the deployed base.
  assetsInclude: ['**/*.whl'],
  server: {
    fs: { allow: ['..'] },
  },
  // Keep the pyembroidery wheel's PEP 427 filename intact — micropip parses it
  // and rejects Vite's `-[hash]` suffix. Everything else stays content-addressed.
  // The wheel is imported from the worker, so the worker pipeline needs the rule
  // too (it has its own rollup config).
  worker: {
    format: 'es',
    rollupOptions: { output: { assetFileNames: keepWheelName } },
  },
  build: {
    target: 'es2022',
    rollupOptions: { output: { assetFileNames: keepWheelName } },
  },
})

function keepWheelName(asset) {
  return asset.name && asset.name.endsWith('.whl')
    ? 'vendor/[name][extname]'
    : 'assets/[name]-[hash][extname]'
}
