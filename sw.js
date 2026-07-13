// 写真配置図システム 共通Service Worker
// 電波の悪い場所でもアプリ自体を起動できるようにするためのオフラインキャッシュ
//
// 2026-07-13修正：HTMLページ自体がキャッシュ優先になっており、
// 新しいバージョンをアップロードしても古い画面が表示され続ける不具合を修正。
// → HTMLは「ネットワーク優先（オフライン時のみキャッシュ）」に変更。
//   ライブラリ・地図タイルは引き続き「キャッシュ優先」（オフライン活用のため）。
const CACHE_NAME = 'haichizu-cache-v2';

// 初回アクセス時にまとめてキャッシュしておく主要ファイル
const CORE_ASSETS = [
  './haichizu.html',
  './haichizu_simple.html',
  './haichizu_v2.html',
  './haichizu_v2_pc.html',
  './saigai-form.html',
  './tatechikumoku.html',
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

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  const accept = event.request.headers.get('accept') || '';
  const isHTML = event.request.mode === 'navigate' || accept.includes('text/html');

  if (isHTML) {
    // HTMLページ本体：ネットワーク優先。オンラインなら常に最新版を取得する。
    // オフライン時（取得失敗時）のみ、直近にキャッシュされた版を表示する。
    event.respondWith(
      fetch(event.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // ライブラリ・地図タイル等：キャッシュ優先（オフラインでの再利用のため）
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
