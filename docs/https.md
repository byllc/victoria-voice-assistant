# HTTPS for the web interface

Browsers allow the microphone only on secure pages (HTTPS, or `http://localhost`). So voice from a phone or another computer on the LAN needs the web interface on HTTPS. The certificate is supplied by Bill (decisions log, 2026-10-30). The project does not create or renew it.

## Where the files go

| File | Default location | Override |
|---|---|---|
| Certificate (PEM; full chain if there is an intermediate) | `config/tls/cert.pem` | `ASSISTANT_TLS_CERT` |
| Private key (PEM, unencrypted) | `config/tls/key.pem` | `ASSISTANT_TLS_KEY` |

`config/` here means the configuration directory: `config/` in the repository, or whatever `ASSISTANT_CONFIG_DIR` points to (the LaunchAgent sets it). `config/tls/` is git-ignored, so neither file is ever committed.

Behaviour of `assistant web`:

- **Both files present:** serves HTTPS and listens on all interfaces (`0.0.0.0`) so LAN devices can connect. Session cookies get the `Secure` flag.
- **Neither file present:** serves plain HTTP on `127.0.0.1`, as in Stage 3. Voice still works in a browser on the same Mac at `http://localhost:8766`.
- **Only one file, a key that doesn't match the certificate, or unreadable files:** refuses to start and says why. It never quietly falls back to HTTP after a bad update.

`--host` or `ASSISTANT_WEB_HOST` overrides the bind address in either case.

## Certificate requirements

- **Names:** the certificate's subjectAltName must include the name or address the household's browsers use, for example `victoria.home.arpa` or `192.168.1.20`. Browsers ignore the subject's common name.
- **Trust:** every household device must trust the issuer. A public certificate (for example from Let's Encrypt for a real domain) works everywhere. A private CA's root must be installed on each phone and computer. On iOS: install the profile, then turn it on under Settings > General > About > Certificate Trust Settings.
- **Key format:** the private key must not have a passphrase, because the service starts unattended under launchd.

## Install or update the certificate

The steps are the same for the first install and every renewal.

1. Copy the new files into place, with the key readable only by you:

   ```sh
   mkdir -p config/tls
   cp /path/to/fullchain.pem config/tls/cert.pem
   cp /path/to/privkey.pem   config/tls/key.pem
   chmod 600 config/tls/key.pem
   ```

2. Check the key matches and the names and expiry are right:

   ```sh
   uv run assistant tls status
   ```

   This prints the subject, names, expiry date and days left. It fails with a message if the key doesn't match or a file is missing.

3. Restart the web process. Certificates are read only at startup, and the assistant service doesn't need a restart.

   - Under launchd:

     ```sh
     launchctl kickstart -k gui/$(id -u)/com.victoria.voice-assistant-web
     ```

   - Run by hand: stop `uv run assistant web`, then start it again.

4. Confirm: open `https://<name>:8766` on a phone and check the browser shows no certificate warning. The web log has a `tls_enabled` event with the names and expiry.

## Expiry

At startup the web process logs `tls_expiring` (warning) when fewer than 30 days remain, and `tls_expired` (error) once the certificate has expired. `assistant tls status` shows the days left at any time. Let's Encrypt certificates last 90 days, so a renewal needs steps 1–3 each time. Any automation that renews the files should run `launchctl kickstart -k …` afterwards.

## Going back to HTTP

Remove (or move away) both files in `config/tls/` and restart the web process. It returns to plain HTTP on `127.0.0.1`.
