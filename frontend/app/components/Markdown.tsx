import { createElement, type ReactNode } from "react";

// Minimal, dependency-free Markdown renderer for previews (the project carries no markdown lib and
// hand-rolls its primitives). Builds React nodes — text is escaped by React, so there's no
// dangerouslySetInnerHTML / XSS surface.
//
// Block: fenced code, ATX headings, hr, blockquote, ordered/unordered lists, pipe tables, paragraphs.
// Inline: **bold**, *italic*, `code`, [links](url). Emphasis is ASTERISK-only — underscores are
// left literal so identifiers (tool_call_names, snake_case) in prompts don't get italicised.

type Props = { content: string; className?: string };

const INLINE_RE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)]+\))/g;

function inline(text: string, keyp: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  INLINE_RE.lastIndex = 0;
  while ((m = INLINE_RE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    const key = `${keyp}i${i++}`;
    if (m[1]) {
      out.push(
        <code key={key} className="rounded bg-hilite/[0.07] px-1 py-px font-mono text-[10.5px] text-fg">
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (m[2]) {
      out.push(<strong key={key} className="font-semibold text-fg">{tok.slice(2, -2)}</strong>);
    } else if (m[3]) {
      out.push(<em key={key}>{tok.slice(1, -1)}</em>);
    } else {
      const lm = /\[([^\]]+)\]\(([^)]+)\)/.exec(tok)!;
      out.push(
        <a key={key} href={lm[2]} target="_blank" rel="noreferrer" className="text-signal hover:underline">
          {lm[1]}
        </a>,
      );
    }
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// A paragraph keeps its internal single newlines as <br/> — prompt previews are line-oriented
// (User: … / Assistant: … on their own lines), so collapsing them to spaces reads worse.
function multiline(text: string, keyp: string): ReactNode[] {
  const lines = text.split("\n");
  const out: ReactNode[] = [];
  lines.forEach((ln, idx) => {
    if (idx > 0) out.push(<br key={`${keyp}br${idx}`} />);
    out.push(...inline(ln, `${keyp}l${idx}`));
  });
  return out;
}

const HEAD_CLS = [
  "text-[13px] font-semibold text-fg mt-2.5 mb-1",
  "text-[12.5px] font-semibold text-fg mt-2 mb-1",
  "text-[12px] font-semibold text-fg-muted mt-1.5 mb-0.5",
  "text-[11.5px] font-semibold text-fg-muted mt-1.5 mb-0.5",
  "text-[11.5px] font-semibold text-fg-faint mt-1 mb-0.5",
  "text-[11px] font-semibold text-fg-faint mt-1 mb-0.5",
];

const SPECIAL = /^(```|#{1,6}\s|>\s?|\s*[-*+]\s+|\s*\d+\.\s+|\s*\|)/;
const HR = /^\s*([-*_])\1{2,}\s*$/;
const ROW = /^\s*\|/;
const DIVIDER = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

const cells = (line: string) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

function parseBlocks(src: string): ReactNode[] {
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let k = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line)) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; // closing fence
      blocks.push(
        <pre
          key={k++}
          className="overflow-auto rounded-md border border-line bg-ink-950/70 p-2 font-mono text-[10.5px] leading-relaxed text-fg-muted"
        >
          {buf.join("\n")}
        </pre>,
      );
      continue;
    }

    if (line.trim() === "") {
      i++;
      continue;
    }

    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length;
      blocks.push(
        createElement(
          `h${level}`,
          { key: k++, className: HEAD_CLS[level - 1] },
          inline(h[2], `h${k}`),
        ),
      );
      i++;
      continue;
    }

    if (HR.test(line)) {
      blocks.push(<hr key={k++} className="my-2.5 border-line" />);
      i++;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      blocks.push(
        <blockquote key={k++} className="border-l-2 border-line pl-2.5 text-fg-faint">
          {parseBlocks(buf.join("\n"))}
        </blockquote>,
      );
      continue;
    }

    // A pipe table. The first row is the header only when a |---| line follows it; every line
    // that starts with a pipe is consumed here, so a stray one can never stall the loop.
    if (ROW.test(line)) {
      const rows: string[][] = [];
      while (i < lines.length && ROW.test(lines[i])) rows.push(cells(lines[i++]));
      const head = rows.length > 1 && DIVIDER.test(`|${rows[1].join("|")}|`) ? rows.splice(0, 2)[0] : null;
      const tk = k++;
      blocks.push(
        <div key={tk} className="my-1.5 overflow-x-auto">
          <table className="w-full border-collapse text-left text-[11px] leading-snug text-fg-muted">
            {head && (
              <thead>
                <tr>
                  {head.map((c, j) => (
                    <th key={j} className="whitespace-nowrap border-b border-line px-1.5 py-1 font-semibold text-fg">
                      {inline(c, `th${tk}-${j}`)}
                    </th>
                  ))}
                </tr>
              </thead>
            )}
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>
                  {r.map((c, j) => (
                    <td key={j} className="border-b border-line/50 px-1.5 py-1 align-top">
                      {inline(c, `td${tk}-${ri}-${j}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items: ReactNode[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(<li key={items.length}>{inline(lines[i].replace(/^\s*[-*+]\s+/, ""), `ul${k}-${items.length}`)}</li>);
        i++;
      }
      blocks.push(
        <ul key={k++} className="list-disc space-y-0.5 pl-4 text-[11px] leading-relaxed text-fg-muted">
          {items}
        </ul>,
      );
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items: ReactNode[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(<li key={items.length}>{inline(lines[i].replace(/^\s*\d+\.\s+/, ""), `ol${k}-${items.length}`)}</li>);
        i++;
      }
      blocks.push(
        <ol key={k++} className="list-decimal space-y-0.5 pl-4 text-[11px] leading-relaxed text-fg-muted">
          {items}
        </ol>,
      );
      continue;
    }

    // paragraph: gather consecutive plain lines
    const buf: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !SPECIAL.test(lines[i]) &&
      !HR.test(lines[i])
    ) {
      buf.push(lines[i++]);
    }
    blocks.push(
      <p key={k++} className="text-[11px] leading-relaxed text-fg-muted">
        {multiline(buf.join("\n"), `p${k}`)}
      </p>,
    );
  }

  return blocks;
}

export function Markdown({ content, className }: Props) {
  return <div className={className}>{parseBlocks(content || "")}</div>;
}
