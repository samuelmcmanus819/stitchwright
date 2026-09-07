/* StitchWright service worker.
 *
 * Goal: make repeat visits instant and allow offline use once warmed, by
 * caching the two big immutable payloads — our fingerprinted build assets and
 * the versioned Pyodide files on the CDN. Bump CACHE (and PYODIDE_VERSION in
 * src/worker.js) together so clients drop the old set.
 */
const CACHE = 'stitchwright-v1-pyodide-314.0.6'

const CACHEABLE_HOSTS = [
  'cdn.jsdelivr.net', // Pyodide runtime + package wheels
  'files.pythonhosted.org', // fallback if a wheel is ever fetched from PyPI
]

self.addEventListener('install', (event) => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys()
      await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      await self.clients.claim()
      // The first navigation happens before this SW controls the page, so the
      // fetch handler never sees it. Warm it now so a later offline visit has
      // an app shell to boot from. (The page caches its own hashed JS/CSS —
      // see the SW registration in main.js — since only it knows those names.)
      try {
        await (await caches.open(CACHE)).add('./')
      } catch {
        /* offline on first run, or blocked — the online path will fill it */
      }
    })(),
  )
})

function isCacheable(url) {
  if (url.origin === self.location.origin) return true
  return CACHEABLE_HOSTS.includes(url.hostname)
}

self.addEventListener('fetch', (event) => {
  const { request } = event
  if (request.method !== 'GET') return

  const url = new URL(request.url)

  // HTML navigations: network-first so a new deploy shows up immediately,
  // with the cache as an offline fallback.
  if (request.mode === 'navigate') {
    event.respondWith(
      (async () => {
        try {
          const fresh = await fetch(request)
          const cache = await caches.open(CACHE)
          cache.put(request, fresh.clone())
          return fresh
        } catch {
          return (
            (await caches.match(request, { ignoreSearch: true, ignoreVary: true })) ||
            (await caches.match('./', { ignoreVary: true })) ||
            (await caches.match('./index.html')) ||
            new Response('Offline and not cached yet.', {
              status: 503,
              headers: { 'Content-Type': 'text/plain' },
            })
          )
        }
      })(),
    )
    return
  }

  if (!isCacheable(url)) return

  // Immutable, content-addressed assets: cache-first. ignoreVary because Vite's
  // preview server and some CDNs send `Vary` headers that would otherwise make
  // a replayed request miss its stored response.
  event.respondWith(
    (async () => {
      const cached = await caches.match(request, { ignoreVary: true, ignoreSearch: true })
      if (cached) return cached
      const response = await fetch(request)
      // status 200 only: the Cache API rejects 206 partial responses.
      if (response.status === 200 || response.type === 'opaque') {
        const cache = await caches.open(CACHE)
        cache.put(request, response.clone())
      }
      return response
    })(),
  )
})
