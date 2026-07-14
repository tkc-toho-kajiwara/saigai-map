// 現地記録票フォーム（saigai-form.html）専用 Service Worker
//
// 2026-07-14修正：HTMLページ自体がキャッシュ優先になっており、
// 新しいバージョンをアップロードしても古い画面が表示され続ける不具合を修正。
// （haichizuシリーズ共通のsw.jsで見つかったのと同じ不具合）
// → HTMLは「ネットワーク優先（オフライン時のみキャッシュ）」に変更。
//   その他の静的ファイル（ライブラリ・アイコン等）は引き続きキャッシュ優先。
const CACHE_NAME = 'saigai-form-v16';
const ASSETS = [
  './saigai-form.html',
  './manifest.json',
  './html2canvas.min.js',
  './jsQR.min.js',
  './icons/icon-192.png',
  './icons/icon-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache =>
      Promise.all(
        ASSETS.map(url =>
          fetch(url, { mode: 'no-cors' }).then(res => cache.put(url, res)).catch(() => {})
        )
      )
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;

  const accept = event.request.headers.get('accept') || '';
  const isHTML = event.request.mode === 'navigate' || accept.includes('text/html');

  if (isHTML) {
    // HTMLページ本体：ネットワーク優先。オンラインなら常に最新版を取得する。
    event.respondWith(
      fetch(event.request)
        .then(res => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // その他の静的ファイル：キャッシュ優先（オフライン活用のため）
  event.respondWith(
    caches.match(event.request).then(cached => {
      const fetchPromise = fetch(event.request)
        .then(res => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy)).catch(() => {});
          return res;
        })
        .catch(() => cached);
      return cached || fetchPromise;
    })
  );
});
