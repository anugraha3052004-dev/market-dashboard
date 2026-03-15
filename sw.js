// India Market Dashboard — Service Worker v2
const CACHE = 'imd-v2';
const STATIC = [
  '/',
  '/index.html',
  '/manifest.json',
  'https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600&display=swap',
  'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js'
];

// Install — cache static assets
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => {
      return Promise.allSettled(STATIC.map(url =>
        c.add(url).catch(() => console.log('SW: skip', url))
      ));
    }).then(() => self.skipWaiting())
  );
});

// Activate — delete old caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch strategy:
// - API calls (/stock, /commodities, /news etc.) → Network first, no cache
// - Static assets → Cache first, then network
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API endpoints — always go to network (real-time data)
  const apiPaths = ['/stock','/stocks','/commodities','/futures','/news','/market-news','/scan','/search','/etfs','/health','/history'];
  if (apiPaths.some(p => url.pathname.startsWith(p))) {
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(JSON.stringify({error:'Offline — no data available'}),
          {headers:{'Content-Type':'application/json'}})
      )
    );
    return;
  }

  // Static assets — cache first
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(resp => {
        if (resp && resp.status === 200 && resp.type !== 'opaque') {
          const clone = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return resp;
      }).catch(() => cached || new Response('Offline', {status: 503}));
    })
  );
});
