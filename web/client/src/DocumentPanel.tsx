import { useCallback, useEffect, useState } from "react";

import { Button } from "@pipecat-ai/voice-ui-kit";

import { pageAddress } from "./links";
import type { PageView } from "./types";

type Entry = { url: string; title: string | null };

/** What the panel has shown this session (back and forward), and whether it is open. */
export function usePanel() {
  const [history, setHistory] = useState<{ entries: Entry[]; index: number }>({ entries: [], index: -1 });
  const [visible, setVisible] = useState(false);
  const { entries, index } = history;

  const open = useCallback((url: string, title: string | null = null) => {
    setVisible(true);
    setHistory(({ entries, index }) =>
      // The same page again (a click twice, or show_page over both chat and voice) is not a new step.
      entries[index]?.url === url
        ? { entries, index }
        : { entries: [...entries.slice(0, index + 1), { url, title }], index: index + 1 },
    );
  }, []);

  return {
    current: entries[index] ?? null,
    visible,
    canBack: index > 0,
    canForward: index < entries.length - 1,
    open,
    back: () => setHistory((h) => ({ ...h, index: Math.max(0, h.index - 1) })),
    forward: () => setHistory((h) => ({ ...h, index: Math.min(h.entries.length - 1, h.index + 1) })),
    show: () => setVisible(true),
    close: () => setVisible(false),
  };
}

export type Panel = ReturnType<typeof usePanel>;

type Load = { url: string; view: PageView | null; failed: string | null };

/**
 * The document panel: the right half of a wide screen, or the whole screen on a phone.
 * A page that allows framing shows live, in a sandboxed frame; one that refuses (most big sites), or plain
 * http on this https page, shows as a reader view: its text, fetched through the assistant's page reader.
 */
export function DocumentPanel({ panel }: { panel: Panel }) {
  const url = panel.current?.url ?? null;
  const [address, setAddress] = useState(url ?? "");
  const [problem, setProblem] = useState("");
  const [load, setLoad] = useState<Load | null>(null);
  const [reader, setReader] = useState(false);

  useEffect(() => {
    setAddress(url ?? "");
    setProblem("");
    setReader(false);
    if (!url) return;
    let cancelled = false;
    setLoad({ url, view: null, failed: null });
    fetch(`/api/page?url=${encodeURIComponent(url)}`, { credentials: "same-origin" })
      .then(async (response) => {
        if (!response.ok) throw new Error(response.status === 401 ? "signed out" : "the assistant couldn't fetch it");
        return (await response.json()) as PageView;
      })
      .then((view) => !cancelled && setLoad({ url, view, failed: null }))
      .catch((error: Error) => !cancelled && setLoad({ url, view: null, failed: error.message }));
    return () => {
      cancelled = true;
    };
  }, [url]);

  const go = (event: React.FormEvent) => {
    event.preventDefault();
    const target = pageAddress(address);
    if (target) panel.open(target);
    else setProblem("Enter a web address, like bbc.co.uk/weather.");
  };

  const view = load?.url === url ? load.view : null;
  const mixed = location.protocol === "https:" && url?.startsWith("http:");
  const live = Boolean(view?.frameable) && !mixed;
  const showLive = live && !reader;
  const title = panel.current?.title || view?.title || url;

  return (
    <aside
      aria-label="Document panel"
      className="flex flex-col bg-[var(--color-background)] max-lg:fixed max-lg:inset-0 max-lg:z-40 lg:h-full lg:w-1/2 lg:border-l lg:border-current/15"
      style={{ paddingTop: "env(safe-area-inset-top)", paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <div className="flex items-center gap-1 border-b border-current/15 p-2">
        <Button size="sm" variant="ghost" onClick={panel.back} disabled={!panel.canBack} aria-label="Back">
          ←
        </Button>
        <Button size="sm" variant="ghost" onClick={panel.forward} disabled={!panel.canForward} aria-label="Forward">
          →
        </Button>
        <form onSubmit={go} className="min-w-0 flex-1">
          <input
            type="text"
            inputMode="url"
            value={address}
            onChange={(event) => setAddress(event.target.value)}
            placeholder="Web address"
            aria-label="Web address"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            className="w-full rounded-md border border-current/20 bg-transparent px-2 py-1 text-sm"
          />
        </form>
        {live && (
          <Button size="sm" variant="ghost" onClick={() => setReader((r) => !r)} aria-pressed={reader}>
            {reader ? "Live" : "Reader"}
          </Button>
        )}
        {url && (
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-md px-2 py-1 text-sm hover:bg-black/5 dark:hover:bg-white/10"
            aria-label="Open in a new tab"
            title="Open in a new tab"
          >
            ↗
          </a>
        )}
        <Button size="sm" variant="ghost" onClick={panel.close} aria-label="Close the panel">
          ✕
        </Button>
      </div>
      {problem && <p className="px-3 py-1 text-sm text-red-600">{problem}</p>}
      <div className="min-h-0 flex-1">
        {!url ? (
          <p className="p-6 text-center text-sm opacity-60">Type an address above, click a link in the chat, or ask Victoria to show you a page.</p>
        ) : load?.failed ? (
          <Fallback url={url} text={`Couldn't load it: ${load.failed}.`} />
        ) : !view ? (
          <p className="p-6 text-center text-sm opacity-60">Loading…</p>
        ) : showLive ? (
          <iframe
            key={url}
            src={url}
            title={title ?? "Web page"}
            // Scripts run, but the page is another site and can't reach this one; no top-level navigation.
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
            referrerPolicy="no-referrer"
            className="h-full w-full border-0 bg-white"
          />
        ) : view.text ? (
          <article className="h-full overflow-y-auto px-5 py-4">
            <p className="mb-3 text-xs opacity-60">
              Reader view{live ? "" : mixed ? " (the page isn't https, so it can't show live here)" : " (the site doesn't allow showing it live here)"}
            </p>
            {view.title && <h2 className="mb-3 text-xl font-semibold">{view.title}</h2>}
            {view.text.split(/\n+/).map((paragraph, i) => (
              <p key={i} className="mb-3 leading-relaxed">
                {paragraph}
              </p>
            ))}
          </article>
        ) : (
          <Fallback url={url} text={view.error ? `Couldn't show it: ${view.error}.` : "This page has no text to show here."} />
        )}
      </div>
    </aside>
  );
}

function Fallback({ url, text }: { url: string; text: string }) {
  return (
    <div className="flex flex-col items-center gap-3 p-6 text-center text-sm">
      <p className="opacity-70">{text}</p>
      <a href={url} target="_blank" rel="noopener noreferrer" className="underline">
        Open it in a new tab
      </a>
    </div>
  );
}
