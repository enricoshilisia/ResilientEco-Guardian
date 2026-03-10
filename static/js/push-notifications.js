/**
 * static/js/push-notifications.js
 * ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 * ResilientEco Guardian — Browser Push Notification Registration
 *
 * Include on dashboard pages:
 *   <script src="{% static 'js/push-notifications.js' %}"></script>
 *
 * Also register the service worker (see sw.js below this file).
 *
 * Django endpoints used:
 *   GET  /api/push/vapid-public-key/   → { publicKey: "..." }
 *   POST /api/push/subscribe/          → stores subscription
 * ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 */

const PushNotifications = (() => {

  // ── Helpers ──────────────────────────────────────────────────────────────

  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64  = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw     = atob(base64);
    return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
  }

  function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.getAttribute('content');
    const match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? match[1] : '';
  }

  function isSupported() {
    return 'serviceWorker' in navigator
      && 'PushManager' in window
      && 'Notification' in window;
  }

  // ── Core functions ────────────────────────────────────────────────────────

  async function getVapidPublicKey() {
    const resp = await fetch('/api/push/vapid-public-key/');
    if (!resp.ok) throw new Error('VAPID key endpoint unavailable');
    const { publicKey } = await resp.json();
    return publicKey;
  }

  async function registerServiceWorker() {
    return navigator.serviceWorker.register('/static/js/sw.js', { scope: '/' });
  }

  async function subscribe() {
    if (!isSupported()) {
      console.warn('[PushNotifications] Browser does not support push');
      return null;
    }

    // 1. Request notification permission
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      console.info('[PushNotifications] Permission denied');
      return null;
    }

    try {
      // 2. Get VAPID key from Django
      const vapidPublicKey = await getVapidPublicKey();

      // 3. Register (or get existing) service worker
      const registration = await registerServiceWorker();
      await navigator.serviceWorker.ready;

      // 4. Subscribe with VAPID
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly:      true,
        applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
      });

      // 5. Send subscription to Django
      const resp = await fetch('/api/push/subscribe/', {
        method:  'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken':  getCsrfToken(),
        },
        body: JSON.stringify({ subscription: subscription.toJSON() }),
      });

      if (!resp.ok) throw new Error(`Subscribe failed: ${resp.status}`);

      console.info('[PushNotifications] Subscribed successfully');
      return subscription;

    } catch (err) {
      console.error('[PushNotifications] Subscribe error:', err);
      return null;
    }
  }

  async function unsubscribe() {
    if (!isSupported()) return;
    try {
      const reg = await navigator.serviceWorker.getRegistration('/');
      if (!reg) return;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        await sub.unsubscribe();
        console.info('[PushNotifications] Unsubscribed');
      }
    } catch (err) {
      console.warn('[PushNotifications] Unsubscribe error:', err);
    }
  }

  async function getStatus() {
    if (!isSupported()) return 'unsupported';
    const permission = Notification.permission;
    if (permission === 'denied') return 'denied';

    try {
      const reg = await navigator.serviceWorker.getRegistration('/');
      if (!reg) return 'unregistered';
      const sub = await reg.pushManager.getSubscription();
      return sub ? 'subscribed' : 'unsubscribed';
    } catch {
      return 'unknown';
    }
  }

  // ── UI helper — renders a "Enable alerts" button ─────────────────────────

  function renderToggle(containerId) {
    const container = document.getElementById(containerId);
    if (!container || !isSupported()) return;

    const btn = document.createElement('button');
    btn.className = 'push-toggle-btn';
    container.appendChild(btn);

    async function refresh() {
      const status = await getStatus();
      btn.dataset.status = status;
      btn.textContent = {
        subscribed:   'Notifications On',
        unsubscribed: 'Enable Alerts',
        denied:       'Notifications Blocked',
        unsupported:  'Push Unsupported',
        unknown:      'Enable Alerts',
      }[status] || 'Enable Alerts';
      btn.disabled = status === 'denied' || status === 'unsupported';
    }

    btn.addEventListener('click', async () => {
      const status = await getStatus();
      if (status === 'subscribed') {
        await unsubscribe();
      } else {
        await subscribe();
      }
      await refresh();
    });

    refresh();
  }

  return { subscribe, unsubscribe, getStatus, renderToggle, isSupported };
})();

// Auto-prompt on dashboard if user has not yet decided
document.addEventListener('DOMContentLoaded', () => {
  // Only prompt if user has not been asked before (stored in sessionStorage)
  if (
    PushNotifications.isSupported() &&
    Notification.permission === 'default' &&
    !sessionStorage.getItem('push-prompt-shown')
  ) {
    sessionStorage.setItem('push-prompt-shown', '1');
    // Small delay so it doesn't compete with the page load
    setTimeout(() => PushNotifications.subscribe(), 3000);
  }

  // Render toggle in any element with id="push-toggle-container"
  PushNotifications.renderToggle('push-toggle-container');
});