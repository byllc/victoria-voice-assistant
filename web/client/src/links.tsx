import type { ReactNode } from "react";

/** Markdown links [title](https://…) and bare https://… addresses in a reply. */
const LINK = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"'()]*[^\s<>"'().,;:!?])/g;

export type Segment = { text: string; url?: string };

/** Splits text into plain runs and links. */
export function linkSegments(text: string): Segment[] {
  const segments: Segment[] = [];
  let last = 0;
  for (const match of text.matchAll(LINK)) {
    const at = match.index ?? 0;
    if (at > last) segments.push({ text: text.slice(last, at) });
    const url = match[2] ?? match[3];
    segments.push({ text: match[1] ?? url, url });
    last = at + match[0].length;
  }
  if (last < text.length) segments.push({ text: text.slice(last) });
  return segments;
}

/**
 * A web address from the address bar: `bbc.co.uk/weather` becomes https://…; anything else is refused (null).
 * The same rule as the server's `page_address` (tools/pages.py).
 */
export function pageAddress(input: string): string | null {
  let value = input.trim();
  if (!value || /\s/.test(value)) return null;
  if (!value.includes("://")) value = `https://${value}`;
  try {
    const url = new URL(value);
    if ((url.protocol !== "http:" && url.protocol !== "https:") || !url.hostname.includes(".")) return null;
    return url.href;
  } catch {
    return null;
  }
}

/** Text with its links made clickable: a plain click opens the document panel, cmd/ctrl-click a new tab. */
export function Linked({ text, onOpen }: { text: string; onOpen: (url: string) => void }): ReactNode {
  return linkSegments(text).map((segment, i) =>
    segment.url ? (
      <a
        key={i}
        href={segment.url}
        target="_blank"
        rel="noopener noreferrer"
        className="underline decoration-current/40 underline-offset-2 hover:decoration-current"
        onClick={(event) => {
          if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
          event.preventDefault();
          onOpen(segment.url!);
        }}
      >
        {segment.text}
      </a>
    ) : (
      segment.text
    ),
  );
}
