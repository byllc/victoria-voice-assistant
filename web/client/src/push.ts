// Web Push for the installed app (architecture §10.3, docs/push.md).

// This browser's own id, so the assistant only pushes to browsers that aren't open.
export const clientId = (() => {
  try {
    const saved = localStorage.getItem("victoria-client-id");
    if (saved) return saved;
    const made = crypto.randomUUID();
    localStorage.setItem("victoria-client-id", made);
    return made;
  } catch {
    return crypto.randomUUID();
  }
})();

const pushSupported =
  "serviceWorker" in navigator && "PushManager" in window && "Notification" in window && window.isSecureContext;
const iosNotInstalled =
  /iPhone|iPad/.test(navigator.userAgent) && !(navigator as Navigator & { standalone?: boolean }).standalone;

// The service worker that shows pushes; null where it can't be registered (then no alerts are offered).
const workerRegistration: Promise<ServiceWorkerRegistration | null> = pushSupported
  ? navigator.serviceWorker.register("/sw.js").catch(() => null)
  : Promise.resolve(null);

function urlBase64ToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = (value + "=".repeat((4 - (value.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

export type PushState = { available: boolean; subscribed: boolean; hint: string | null };

export async function currentSubscription(): Promise<PushSubscription | null> {
  const registration = await workerRegistration;
  return registration ? registration.pushManager.getSubscription() : null;
}

/** Whether alerts can be offered here, and if not, why. `publicKey` is null when push is off on the assistant. */
export async function pushState(publicKey: string | null): Promise<PushState> {
  if (!publicKey) return { available: false, subscribed: false, hint: null };
  if (!pushSupported || !(await workerRegistration)) {
    const hint = window.isSecureContext ? "This browser can't receive alerts." : "Alerts need the HTTPS address.";
    return { available: false, subscribed: false, hint };
  }
  if (iosNotInstalled) {
    return { available: false, subscribed: false, hint: "For alerts on iPhone: Share → Add to Home Screen, then open Victoria from there." };
  }
  return { available: true, subscribed: Boolean(await currentSubscription()), hint: null };
}

/** Turn alerts on (asking permission) or off. Returns the subscription to send to the assistant, if any. */
export async function togglePush(publicKey: string): Promise<{ subscribe?: PushSubscriptionJSON; unsubscribe?: string; hint?: string }> {
  const existing = await currentSubscription();
  if (existing) {
    const endpoint = existing.endpoint;
    await existing.unsubscribe();
    return { unsubscribe: endpoint };
  }
  if ((await Notification.requestPermission()) !== "granted") {
    return { hint: "Alerts are blocked for this site in the browser's settings." };
  }
  const registration = await workerRegistration;
  if (!registration) return { hint: "This browser can't receive alerts." };
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToBytes(publicKey),
  });
  return { subscribe: subscription.toJSON() };
}
