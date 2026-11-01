# Phone alerts (Web Push)

The web client can be installed on a phone's home screen and get notifications from the assistant when the page is closed: an agent's result, an error, or an agent's question (architecture §10.3). Push is off until you turn it on.

## What gets pushed, and to whom

- A notification for a device is pushed to each browser that turned on alerts for that device **and doesn't have the page open right now**. A browser with the page open already shows it (chime, card, badge).
- Results, errors and agent questions are pushed. `low` items (system notices) never are.
- **Nothing is pushed during quiet hours or do-not-disturb** (default midnight to 8am, `notifications.quiet_hours` in `tools.yaml`). An agent's question at night only waits on the dashboard (Bill, 2026-10-31).
- Tapping the notification opens the app. Answer questions there, by voice, or in chat ("Tell Hermes ...").
- A browser that turns alerts off, or an app that's deleted, is forgotten the next time a push to it is refused.

The push goes through the browser maker's push service (Apple for Safari and home-screen apps on iPhone, Google for Chrome, Mozilla for Firefox). The content is encrypted for the browser, so the push service can't read it; it does see that a message was sent, and when.

## Turn it on

1. **HTTPS.** Push needs the web interface on HTTPS with a certificate the phone trusts ([https.md](https.md)). On a phone, `http://` won't work.
2. **Create the signing key** (once). It's stored in the Keychain and never printed ([secrets.md](secrets.md)):

   ```sh
   uv run assistant push keys
   ```

   Don't run it again later: a new key cuts off every subscribed browser. (`push keys` refuses to replace an existing key.)
3. **Turn it on in `config/tools.yaml`**:

   ```yaml
   notifications:
     web_push: true
     vapid_private_key: ${VAPID_PRIVATE_KEY:-}
     vapid_subject: mailto:you@example.com   # a contact the push services can reach about our pushes
   ```

   Then restart the assistant. Check with `uv run assistant push status`.
4. **On an iPhone:** open the web client in Safari, tap Share → Add to Home Screen, then open Victoria from the home screen (iOS only allows push for home-screen web apps). Sign in, tap **Phone alerts**, and allow notifications. On Android or a computer, the **Phone alerts** button works in the browser directly.
5. **Test:** close the app on the phone, then on the Mac type "Ask Hermes to ..." in a background task (or any agent in `background` mode). When it finishes, the phone gets "Hermes has finished." with the summary.

`Alerts on` means this browser is subscribed; tap it again to turn alerts off. If the button is missing, the page says why ("Alerts need the HTTPS address", "Add to Home Screen first", or "This browser can't receive alerts").

## Turn it off

Set `web_push: false` and restart. Subscriptions stay stored (`push_subscriptions` in the database) for when it's turned back on; `assistant push status` lists them.
