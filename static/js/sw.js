/**
 * static/js/sw.js  — Service Worker
 * ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 * Handles incoming Web Push events and notification click routing.
 * Must be served from the root path or dashboard scope.
 *
 * Push payload (sent by NotificationDispatcher):
 * {
 *   "title":   "ResilientEco — RED Alert",
 *   "body":    "Nakuru Zone: Flood risk at 87%.",
 *   "icon":    "/static/img/logo-192.png",
 *   "badge":   "/static/img/badge-72.png",
 *   "tag":     "alert-<session_id>",
 *   "data":    { "url": "/dashboard/?alert=abc", "alert_level": "RED" },
 *   "actions": [
 *     { "action": "view",    "title": "View Advisory" },
 *     { "action": "dismiss", "title": "Dismiss" }
 *   ]
 * }
 * ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 */

const CACHE_NAME = 'resilienteco-sw-v1';

// ── Install ───────────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
  self.skipWaiting();
});

// ── Activate ──────────────────────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// ── Push event ────────────────────────────────────────────────────────────────
self.addEventListener('push', event => {
  if (!event.data) return;

  let payload;
  try {
    payload = event.data.json();
  } catch {
    payload = { title: 'ResilientEco Alert', body: event.data.text() };
  }

  const title   = payload.title   || 'ResilientEco Guardian';
  const options = {
    body:    payload.body    || 'New climate alert for your monitoring zone.',
    icon:    payload.icon    || '/static/img/logo-192.png',
    badge:   payload.badge   || '/static/img/badge-72.png',
    tag:     payload.tag     || 'resilienteco-alert',
    renotify: payload.renotify !== false,
    data:    payload.data    || {},
    actions: payload.actions || [
      { action: 'view',    title: 'View Advisory' },
      { action: 'dismiss', title: 'Dismiss' },
    ],
    // Vibrate pattern for mobile: short, pause, long
    vibrate: [100, 50, 200],
    // Keep notification visible until user interacts
    requireInteraction: (payload.data || {}).alert_level === 'RED',
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

// ── Notification click ────────────────────────────────────────────────────────
self.addEventListener('notificationclick', event => {
  event.notification.close();

  if (event.action === 'dismiss') return;

  const targetUrl = (event.notification.data || {}).url || '/dashboard/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(windowClients => {
        // Focus an existing tab if one is open on the same origin
        for (const client of windowClients) {
          if (new URL(client.url).origin === self.location.origin && 'focus' in client) {
            client.navigate(targetUrl);
            return client.focus();
          }
        }
        // Otherwise open a new tab
        if (self.clients.openWindow) {
          return self.clients.openWindow(targetUrl);
        }
      })
  );
});

// ── Push subscription change ──────────────────────────────────────────────────
self.addEventListener('pushsubscriptionchange', event => {
  // Resubscribe and send new subscription to server
  event.waitUntil(
    self.registration.pushManager.subscribe(event.oldSubscription.options)
      .then(subscription => {
        return fetch('/api/push/subscribe/', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ subscription: subscription.toJSON() }),
        });
      })
  );
});