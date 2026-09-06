// Офлайн-режим: в кассе сети нет, а калькулятор обязан работать целиком, а не
// показывать кеш — см. Docs/Wayfinder/Tickets/offline-at-the-counter.md.
//
// Оболочка кешируется cache-first (страница не меняется между выкладками),
// rates.json — network-first: свежий курс важнее скорости, но его отсутствие
// не должно мешать считать руками.
const SHELL_CACHE = 'transfer-calc-shell-v1';
const DATA_CACHE = 'transfer-calc-data-v1';
const SHELL = ['./', './index.html', './sw.js'];
const KEEP = [SHELL_CACHE, DATA_CACHE];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => !KEEP.includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Ответ из кеша помечаем заголовком: страница по нему понимает, что сети нет,
// и пишет «Нет сети» в строку статуса вместо тихой подстановки старых чисел.
function marked(hit) {
  const headers = new Headers(hit.headers);
  headers.set('x-from-cache', '1');
  return hit.blob().then((body) => new Response(body, { status: 200, headers }));
}

self.addEventListener('fetch', (e) => {
  const request = e.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.endsWith('/rates.json')) {
    e.respondWith(
      fetch(request)
        .then((resp) => {
          if (resp.ok) {
            const copy = resp.clone();
            caches.open(DATA_CACHE).then((c) => c.put('./rates.json', copy));
          }
          return resp;
        })
        .catch(() =>
          caches
            .match('./rates.json')
            .then((hit) => (hit ? marked(hit) : new Response('{}', { status: 503 })))
        )
    );
    return;
  }

  e.respondWith(
    caches.match(request).then((hit) => hit || fetch(request))
  );
});
