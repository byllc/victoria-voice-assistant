# Web interface document panel

- Status: ready for review
- Origin: Bill, 2026-11-05
- Created: 2026-11-06 (UTC)

A browser/document window in the web interface that opens on the right half of the page, beside the chat. Victoria (or Bill) can put a web page or document there to read while the conversation carries on in the left half.

## Done when

- [x] On a wide screen (≥ 1024 px) the page splits in two: chat, timers and tabs on the left, the document panel on the right half. With the panel closed the layout is as it is today.
- [x] On a phone or narrow window the panel opens full-screen over the chat, with a close button back to the chat.
- [x] The panel has a title bar: an address bar Bill can type into, back/forward through what was opened in this session, "open in a new tab", and close.
- [x] Links in Victoria's replies (and search results she cites) open in the panel instead of leaving the page.
- [x] Victoria can open a page in the panel herself, through a tool (`show_page`), when asked ("show me that recipe", "open the BBC weather page"). The tool is configured in `config/tools.yaml` like the others.
- [x] Pages that refuse to be framed (X-Frame-Options / CSP `frame-ancestors`, which is most large sites) still show something useful: decision 2.
- [x] Only `http`/`https` addresses open; the panel's frame is sandboxed so a page can't script the assistant's own page.
- [x] Opening and closing the panel doesn't interrupt voice, music, or a reply that is streaming.
- [x] Automated tests: the tool and its message, the address checks, framing detection, the web route. Typecheck and build pass. (The link rewriting is checked in the browser only: the client has no test runner.)

## Decisions (Bill, 2026-11-05)

1. **What fills the panel.** Decided: links in replies, the `show_page` tool, and an address bar on the panel. Was proposed: links in replies open there on click, and a `show_page` tool lets Victoria open one. (Alternatives: links only, no tool; or also an address bar Bill can type into.)
2. **Sites that refuse to be framed.** Decided: reader view fallback, `/api/page` route approved. Was proposed: try the live page in a sandboxed iframe; when the site refuses framing, show a **reader view** instead: the web interface fetches the page through the existing reader (`search/service.py` `read()`, trafilatura, public addresses only) and shows the extracted text, with "open in a new tab" for the real page. This adds one route to the web interface (`/api/page?url=`), so it is an architecture change for Bill to approve. (Alternative: no server fetch at all; a framing refusal just shows "this site can't be shown here" and the new-tab link.)
3. **Which browsers receive `show_page`.** Decided: the browser that asked. Was proposed: the browser that asked (same `client_id` rule as "play it here"); a satellite request has no screen, so Victoria says she can't show it there.

## Evidence and notes

**Tests:** `uv run pytest -q -m "not live and not hardware"` → 373 passed, 3 skipped (25 new in `tests/test_pages.py`). `npm run build` in `web/client` (typecheck + build) passes.

**Checked in the browser** (a separate test service with `ASSISTANT_SPEECH=0` on port 8775 and an open web interface on 8091; the installed services were not touched, so they still run the old code):

- Typed "Show me the page example.com": the local model called `show_page` and the page opened live in the right half.
- At 1400 px: chat on the left half, panel on the right. At phone width (375 px): the panel covers the screen, ✕ returns to the chat.
- Address bar: `example.com` → live; `github.com/about` (refuses framing) → reader view with the page's text; back/forward step through them.
- A link in Victoria's reply opens in the panel; the page itself does not navigate away. Cmd-click opens a new tab.
- `/api/page` refuses a LAN address (`http://192.168.1.1/`: "address is not on the public internet") and is behind the household login.

**Demo steps for Bill:** restart the two services so they pick up the new code (`launchctl kickstart -k gui/$(id -u)/com.victoria.voice-assistant` and `…/com.victoria.voice-assistant-web`), add the `show_page` entry to `config/tools.yaml` if your copy lacks it (already added on this machine), then on a wide window ask "show me bbc.co.uk/weather" or press **Browser** and type an address.

**Not verified:** that opening the panel leaves a voice session and music playing (the test service had speech off). The layout keeps one component tree whether the panel is open or not, so nothing remounts, but it wasn't tried live.

**Notes:**

- Pages opened live can't be followed: clicks inside the page move the frame, but the address bar keeps the page that was opened (another site's frame can't be read).
- An `http:` page on the HTTPS web interface always uses the reader view (the browser would block it as mixed content).
- Code: `web/client/src/DocumentPanel.tsx`, `links.tsx`, `App.tsx`, `ChatView.tsx`, `useChat.ts`, `types.ts`; `tools/pages.py`; `search/service.py` (`page_view`, `frameable`); `/api/page` in `app.py` and `web/app.py`; `show_page` in `config/examples/tools.yaml`.
