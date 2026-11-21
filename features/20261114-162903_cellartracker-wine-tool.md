# Wine: our CellarTracker cellar, through our own MCP server

- Status: in progress
- Phase: current
- Origin: Bill, 2026-11-14 ("I'd like to add an MCP for cellar tracker to victoria so it can track our wine. I want to be able to ask what is avaialble given some information, \"highest rated whites\". ask what bin something is in, etc.."; then "here is an example https://github.com/slavins-co/cellartracker-mcp"; then "I want to be able to mange the whole library, so lets start there, how about we start by creating an mcp that gets and caches the win elist if it has been more than 24 hours since the last time. you grabbed it"; then "we can also ask \"update the wine list\" if we weant it to update now")
- Created: 2026-11-14 (UTC)
- Started: 2026-11-14, branch `feature-upgrade-feature-tool`

The goal is managing the whole cellar by voice and chat. Work goes in steps; each step's criteria are below.

## Done when

Step 1, the MCP server and the cached wine list (started 2026-11-14):

- [x] Our own MCP server, `assistant cellartracker mcp` (stdio, or streamable HTTP with `--http`), in Python inside this package.
- [x] It fetches the wine list (CellarTracker's `List` export) and keeps it on disk. A request uses the saved copy while it's under 24 hours old, and fetches again when it's older.
- [x] "Update the wine list" fetches now, whatever the age (`update_wine_list`).
- [x] When a fetch fails and a saved copy exists, the saved copy is used and the answer says how old it is and why.
- [x] Credentials are `CT_USERNAME` and `CT_PASSWORD` through `assistant secrets set`, never in YAML, logs or errors; the cache files are mode 600 in a mode-700 directory under the data directory.
- [ ] Tests with a made-up export and a fake CellarTracker (no network). Live check: Bill sets the secrets, then `assistant cellartracker update` fetches his list. (Tests done; live check open.)

Step 2, a built-in `wine` tool (Bill, 2026-11-14: "couldn't we just add a built in cellar tracker tool so I don't have to add anotehr process ?"):

- [x] `kind: builtin`, `service: cellartracker`, in Victoria's own process, using step 1's fetch-and-save code directly. No MCP connection and no extra process; the MCP server stays for other clients (Claude Desktop, Claude Code).
- [x] "What are our highest rated whites?", "What bin is the 2019 Lytton Springs in?", "Do we have any Barolo?", "How many bottles do we have?" read the list; "Update the wine list" fetches now.
- [ ] Live: on Bill's real list, by chat, then by voice. (Fetch, chat and voice tried on Bill's CellarTracker test account, one bottle; a misheard grape fixed, below. Not yet tried on the real cellar.)

Later steps (not started; each needs Bill's go-ahead):

- "What should we drink soon?" (drinking windows; the Availability table).
- Changing the cellar ("we opened the Lytton Springs", moving a bottle, adding a purchase). The CSV export is read only, so this needs another way in (below).

## Blocking decisions

Settled 2026-11-14:

1. **MCP server or our own tool.** Our own MCP server (Bill: "creating an mcp"). Written in Python in this package, so it uses uv, our secrets and our tests, not Node. It fetches the same CSV export the example uses.
2. **How fresh.** Fetched again when the saved copy is more than 24 hours old, or right away on "update the wine list" (Bill, both quoted above).
3. **Credentials.** CellarTracker has no keys or read-only logins: the account's username and password go in the URL over HTTPS (the example's README says the same). They're kept like every other secret (ground rules): `uv run assistant secrets set CT_USERNAME` and `CT_PASSWORD`, which Bill types himself. A password not used anywhere else is recommended.

Open, needed for the later steps:

4. **Which score is "highest rated".** Proposal: the community score (CT) first; our own score (MY) for "our favourite"; a critic's only when named. Unscored wines last.
5. **What gets said.** Proposal: at most 10 wines, already sorted, and the model answers in a sentence or two. Bin questions answer directly, like the groceries tool.
6. **Who can use it.** Proposal: any household device.
7. **How to change the cellar.** "Manage the whole library" includes changes, and the export can't make them. CellarTracker has no public API for writing; the choices are its web forms (scripted, fragile, and against the grain of a site without an API) or keeping changes in Victoria. To be looked into before that step.

## What the example is

[slavins-co/cellartracker-mcp](https://github.com/slavins-co/cellartracker-mcp) (MIT, v0.5.4, last commit 2026-10-13, community project, not CellarTracker's) is a Node.js MCP server over stdio. It downloads the account's CSV export (`https://www.cellartracker.com/xlquery.asp`, tables List, Bottles, Availability, Notes, Purchase, Consumed, …), caches it for 24 hours, and answers 11 read-only tools. `search-cellar` can't sort by score. Victoria's MCP support today is Home Assistant only, over HTTP.

## Notes

- No satellites involved, so this is current-phase work.

## Evidence

Step 1, built 2026-11-14:

- `cellartracker/export.py`: `CellarTrackerExport.table(name, force=False)`. Fetches `xlquery.asp` (`Table=List&Location=1`, so each row is one wine in one location and bin) when the saved copy is 24 hours old or more, or when forced. Saved as `List.csv` and `List.json` (the fetch time) in `<data dir>/cellartracker/`, files 600, folder 700; the fetch time is written last. Two requests at once fetch once. Retries network errors and HTTP 5xx within one 50-second budget; a login failure (401/403, or CellarTracker's "not logged in" page) is never retried. Decodes the Content-Type charset, Windows-1252 when none is given (as the example does). All eight export tables are known, for later steps; only `List` is used now.
- The password is in the URL, so: errors carry a status or an exception's class name, never its text; httpx's request log lines for the export are dropped by a filter.
- `cellartracker/server.py`: the MCP server (MCP SDK 2.3, `MCPServer`). Tools: `get_wine_list` (saved copy unless 24 hours old; the main columns, empty ones left out, `full=true` for all; NV for vintage 1001; totals of wines and bottles), `update_wine_list` (fetches now; says `updated: false` and why when it couldn't), `wine_list_status` (fetches nothing). A failure with nothing saved reaches the client as our message.
- CLI: `assistant cellartracker mcp [--http --port 8931]`, `assistant cellartracker update`, `assistant cellartracker status`. `CT_USERNAME` and `CT_PASSWORD` added to the known secrets and `docs/secrets.md`.
- Tests: 22 in `tests/test_cellartracker.py`, fake CellarTracker via `httpx.MockTransport`. `uv run pytest -q`: 610 passed, 3 skipped.
- Stdio round trip with the MCP client (no credentials, empty data directory): the three tools listed, `wine_list_status` → `{saved: false}`, `get_wine_list` → error "CellarTracker isn't set up: store the login with `uv run assistant secrets set CT_USERNAME` …".
- Not yet: a fetch from the real CellarTracker (needs Bill's login), and Victoria calling the server (a later step).

Step 2, built 2026-11-14:

- `tools/wine.py`, `WineTool`. `read`: one entry per wine with its places (location, bin, bottles), narrowed by `color` and `search` words (name, producer, grape, region, vintage, location, bin; accents and case don't matter; "wine", "any", "from" and the like are ignored), sorted by community score (CT), then our score (MY), unscored last. At most 25 wines, with how many more matched. The model answers from it (`forget_after_s: 0`). `update`: fetches now and says it directly ("I've updated the wine list: 7 bottles of 3 wines."), or that it couldn't and how old the kept list is.
- This follows open decisions 4 and 5 as proposed (community score first; the model answers from the list), pending Bill.
- The login is read at each fetch, so setting `CT_USERNAME` and `CT_PASSWORD` needs no restart. Without them the tool says how to set them.
- `wine` added to both `tools.yaml` files. `assistant config check` passes; `assistant tools status`: `wine` ready, 23 tools active.
- Tests: 15 more in `tests/test_cellartracker.py` (37 in all), on the same made-up export. `uv run pytest -q`: 625 passed, 3 skipped. Found and fixed on the way: a list's age was measured with the system clock instead of the export's.
- Routing, `assistant tools test --only wine` against the dev router model: 5/5. "what are our highest rated whites" → `wine(action='read', color='white')`; "what bin is the 2019 Lytton Springs in" → `search='2019 Lytton Springs'`; "do we have any Barolo" → `search='Barolo'`; "how many bottles of wine do we have" → `read`; "update the wine list" → `update`.
- Nothing has touched the real CellarTracker yet: no account login is set. The made-up export follows the example project's column reference (`skills/cellartracker-data/references/schema.md`), not a real download, so the first live fetch is also the check that the columns match.
- The running service needs a restart to load the new code.

Live, 2026-11-14, Bill's CellarTracker test account (one bottle: 1968 Vega-Sicilia Único, Cellar, bin A1):

- First login refused: `CT_USERNAME` was an email address. Bill changed it in the Keychain; `config/.env` still had the old one, so `assistant secrets sync` (only `CT_USERNAME` differed). Then `assistant cellartracker update` at 17:00 UTC: "Wine list updated: 1 rows, 1 bottles."
- The real export's header has the 66 columns the code expects (Location, Bin, Color, Varietal, MasterVarietal, CT, MY, BeginConsume, EndConsume, …). Its charset is ISO-8859-1, and accents come through (León, Único).
- Bill, by voice, office, 12:00-12:01 local: "How many bottles of wine are in my cellar?" → "You have one bottle of wine in your cellar."; "What's the best bottle of wine in my cellar?" → "The best rated wine is the 1968 Bodegas Vega-Sicilia Ribera del Duero Único."
- "Any Tempranillo in my wine cellar?" was heard as "Any tempera Nioh. In my wine cellar," and the model searched `Nioh`: "I don't see any wine with that name." Searching by grape already worked (`Tempranillo` matches Varietal); the words were misheard. Fixed:
  - A search that matches nothing tries close spellings, word by word and two words run together (difflib ratio 0.8 or more): "tempera Nioh" → the Vega-Sicilia (Tempranillo). Marked `no_exact_match` so the model knows.
  - When nothing is close either, the tool gives the model the cellar's grapes, regions and producers (`in_cellar`) to match the user's words against, and says to search again. For `Nioh`: grapes Tempranillo, Tempranillo Blend.
  - The `search` parameter now asks for all the words as said, even when they look misheard; "do we have any Tempranillo" added to the examples.
  - Tests: 43 in `tests/test_cellartracker.py`; `uv run pytest -q`: 631 passed, 3 skipped. Routing, `assistant tools test --only wine`: 6/6.
- Still to try: the Tempranillo question again by voice after the restart. Whisper might also be helped by wine words in its prompt (grapes, producers); not done (see the Whisper prompt feature).

## Corrections, 2026-11-14

Bill: "would be be possilbe to do a close spelling look up and hae it store corrections? so if I say Tempranillo is Tempra Nioh?"; then, offered wine only first (widening to every transcript later, which changes the voice pipeline and needs his go-ahead): "yes, lets try the corrections library".

- `corrections.py`: `config/corrections.yaml`, one section per use (only `wine` so far), `heard: meant`. Heard words are stored lower case, without accents, single-spaced. Read on every use, so a hand edit applies at once; an unreadable file is logged and ignored. Victoria's writes keep a header comment but not others. Git-ignored like the other household config.
- The `wine` tool runs the search words through the corrections first (whole words, longest first) and says which it used (`corrected`). It learns three ways:
  - told: `correct` ("when I say tempra nioh I mean Tempranillo"). `corrections` lists them; `forget` drops one. All three answer directly.
  - a close spelling that found the wine, when the heard phrase fits only one wine word ("tempera nioh" → Tempranillo, "unicko" → Único, in the list's own spelling). Two different close words: nothing learned.
  - the model searching again after `no_match` with `heard` (the words searched before) and finding the wine ("Nioh" → Tempranillo). Nothing learned if the second search finds nothing.
- Tests: 10 more in `tests/test_cellartracker.py` (53). Every test's corrections file is in its temporary folder (`conftest.py`); an early run had written three test words to the real `config/corrections.yaml`, created minutes before by that run, and it was deleted. `uv run pytest -q`: 641 passed, 3 skipped.
- Routing, `assistant tools test --only wine`: 8/8, including "when I say tempra nioh I mean Tempranillo" → `correct(heard='tempra nioh', meant='Tempranillo')` and "what wine corrections do you have" → `corrections`.
- On Bill's test list, with a scratch corrections file (Bill's file was left empty for him to fill): `Nioh` → no match with the grapes; again as `Tempranillo` with `heard: Nioh` → found, learned; `Nioh` → corrected, found; `correct vega sicily → Vega-Sicilia`; `corrections` → "I have 2 wine corrections: …".
- Not checked: whether the dev model passes `heard` on its own when it searches again after no match (the routing examples are one call each). To watch in use. A known rough edge: "tempera nioh" with `nioh` learned becomes "tempera tempranillo", which finds the wine through the close-spelling path rather than exactly.

## The cellar's words in Whisper's prompt, 2026-11-15

Bill: "would a better voice model help with Tempranillo and Rose?" Answer: probably not much. Speech recognition is already Whisper large-v3-turbo, and the mishearing comes from how rarely those words are said, not from the model's size; choosing a model is staging work anyway. Offered instead to put the cellar's words in Whisper's prompt; Bill: "Yes, add them".

- `tools/wine.py`: `prompt_words(rows, meant)` gives what corrections say was meant first, then grapes, regions and producers by bottles held (ties alphabetical), stopping at 400 characters. Whisper keeps 223 tokens of prompt, and wine names cost about a token per three letters. "Blend" is dropped from grape names, and colours, "Unknown" and the like are left out. `PromptWords` reads the saved list (`CellarTrackerExport.saved`, never a fetch) and the `wine` corrections, and works them out again only when either file changes.
- `voice/models.py`: `whisper_prompt(agents, words)` adds the words after the agents' names. `session_prompt` gives voice and satellite sessions a prompt function while the `cellartracker` tool is ready, and the plain agents prompt otherwise. `LockedWhisperSTT` calls that function before each transcription, so a satellite that stays connected picks up a refreshed list or a new correction. If the function fails, the last prompt is kept and the failure is logged.
- The echo filter (`is_prompt_echo`) works unchanged. Near-silence that comes back as the wine list ("Tempranillo, Ribera del Duero, Bodegas Vega-Sicilia.") is dropped, while "Tempranillo." alone or "Do we have any Tempranillo?" is kept. Saying three or more wine words and nothing else would also be dropped; that's unlikely as a real request.
- "Rosé": the tool already treats rose, rosé and pink as one colour and ignores accents, so it isn't added to the prompt.
- On Bill's test list the prompt is now "Victoria. Ask Hermes, ask Claude, ask Alexa. Tempranillo, Castilla y León, Ribera del Duero, Bodegas Vega-Sicilia."
- Tests: 4 more (prompt words, `PromptWords` following the list and the corrections, `session_prompt` on and off, the echo of wine words, and a prompt function asked per transcription). `uv run pytest -q`: 645 passed, 3 skipped. Assistant restarted; speech ready, office satellite reconnected.
- Not checked: whether Bill's own voice now gets "Tempranillo" right, and whether the longer prompt makes Whisper put wine words into unrelated speech. Both to watch in use, and to judge on the Mac mini.
