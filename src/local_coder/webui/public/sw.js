// Keep API responses and credentials out of caches. The shell needs a live engine.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('local-coder')).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
