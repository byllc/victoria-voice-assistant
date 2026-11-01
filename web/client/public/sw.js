// Service worker: shows Web Push notifications from the assistant (architecture §10.3) and opens the app on tap.
self.addEventListener("push", (event) => {
  let data = { title: "Victoria", body: "", tag: undefined, url: "/" };
  try {
    data = { ...data, ...event.data.json() };
  } catch {
    // not JSON: show the generic title
  }
  event.waitUntil(
    self.registration.showNotification(data.title, { body: data.body, tag: data.tag, data: { url: data.url }, icon: "/icon-192.png" }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url ?? "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const open = windows.find((w) => new URL(w.url).pathname === "/");
      return open ? open.focus() : self.clients.openWindow(url);
    }),
  );
});
