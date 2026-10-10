// Convivy team-site service worker (ARCH-362 Phase 4b).
//
// Its ONLY job is Web Push. It deliberately does NOT cache or intercept fetches:
// this is an authenticated internal site behind CF Access, and a cache would be
// a place where one person's tier-gated page could be served to the next viewer
// of the same browser. Offline support is not worth that class of bug here.
//
// Served from the ORIGIN ROOT (/sw.js) rather than /assets/, because a service
// worker's default scope is its own directory — registered from /assets/ it
// could not control the site.

self.addEventListener('install', function (ev) {
  // Take over immediately rather than waiting for every open tab to close;
  // otherwise the first push after registration silently goes nowhere.
  self.skipWaiting();
});

self.addEventListener('activate', function (ev) {
  ev.waitUntil(self.clients.claim());
});

self.addEventListener('push', function (ev) {
  var data = {};
  try {
    data = ev.data ? ev.data.json() : {};
  } catch (e) {
    data = {};
  }
  var title = data.title || 'Convivy';
  var body = data.body || 'New alert';
  var url = data.url || '/engineering/alerts';
  ev.waitUntil(
    self.registration.showNotification(title, {
      body: body,
      icon: '/assets/icon-192.png',
      badge: '/assets/icon-192.png',
      // Collapse repeats of the same signal into one notification rather than
      // stacking duplicates if a retry lands twice.
      tag: data.signal_id || 'convivy-alert',
      renotify: false,
      data: { url: url }
    })
  );
});

self.addEventListener('notificationclick', function (ev) {
  ev.notification.close();
  var url = (ev.notification.data && ev.notification.data.url) || '/engineering/alerts';
  ev.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      // Focus an already-open tab if there is one, rather than piling up windows
      // every time an alert is tapped.
      for (var i = 0; i < list.length; i++) {
        if (list[i].url.indexOf(url) !== -1 && 'focus' in list[i]) return list[i].focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
