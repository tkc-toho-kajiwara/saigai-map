// 写真配置図システム 共通Service Worker
// 電波の悪い場所でもアプリ自体を起動できるようにするためのオフラインキャッシュ
const CACHE_NAME = 'haichizu-cache-v1';

// 初回アクセス時にまとめてキャッシュしておく主要ファイル
const CORE_ASSETS = [
  './haichizu.html',
  './haichizu_simple.html',
  './haichizu_v2.html',
  './haichizu_v2_pc.html',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
  'https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return Promise.all(
        CORE_ASSETS.map((url) =>
          fetch(url, { mode: 'no-cors' })
            .then((res) => cache.put(url, res))
            .catch(() => {}) // 個別の取得失敗は無視して続行
        )
      );
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// キャッシュ優先。無ければネットワーク取得し、取得できたものは以後のためにキャッシュへ追記
// （地図タイル画像なども閲覧した範囲は自動でオフライン対応されていく）
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fetchPromise = fetch(event.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy)).catch(() => {});
          return res;
        })
        .catch(() => cached);
      return cached || fetchPromise;
    })
  );
});
