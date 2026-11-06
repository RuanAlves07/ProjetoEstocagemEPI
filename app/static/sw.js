// Service Worker para PWA e Notificações Push Mobile
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data = { title: 'Novo Pedido de EPI', body: event.data.text() };
    }
  }

  const title = data.title || '🚨 Novo Pedido de EPI!';
  const options = {
    body: data.body || 'Uma nova solicitação de EPI foi recebida.',
    icon: data.icon || '/static/icone.png',
    badge: data.badge || '/static/icone.png',
    data: {
      url: data.url || '/pedidos'
    },
    tag: data.tag || 'novo-pedido',
    renotify: true,
    requireInteraction: true,
    vibrate: data.vibrate || [200, 100, 200, 100, 200]
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const urlToOpen = event.notification.data && event.notification.data.url
    ? event.notification.data.url
    : '/pedidos';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      for (let client of windowClients) {
        if (client.url.includes(urlToOpen) && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(urlToOpen);
      }
    })
  );
});
