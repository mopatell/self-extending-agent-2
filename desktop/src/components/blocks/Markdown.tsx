/** Enough markdown for agent answers: headings, bullets, code, tables, bold, inline code. ~60 lines. */

import { Fragment, type ReactNode } from "react";

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    out.push(t.startsWith("`") ? <code key={i++}>{t.slice(1, -1)}</code> : <strong key={i++}>{t.slice(2, -2)}</strong>);
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.replace(/\r/g, "").split("\n");
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) body.push(lines[i++]);
      i++;
      blocks.push(<pre key={k++}>{body.join("\n")}</pre>);
    } else if (/^\s*[-*] /.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*] /.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*] /, ""));
      blocks.push(
        <ul key={k++} className="my-1 list-disc pl-6">
          {items.map((it, j) => <li key={j}>{inline(it)}</li>)}
        </ul>,
      );
    } else if (/^\s*\d+\. /.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\. /.test(lines[i])) items.push(lines[i++].replace(/^\s*\d+\. /, ""));
      blocks.push(
        <ol key={k++} className="my-1 list-decimal pl-6">
          {items.map((it, j) => <li key={j}>{inline(it)}</li>)}
        </ol>,
      );
    } else if (line.trim().startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const cells = lines[i++].trim().slice(1, -1).split("|").map((c) => c.trim());
        if (!cells.every((c) => /^:?-+:?$/.test(c))) rows.push(cells);
      }
      blocks.push(
        <table key={k++} className="my-2 border-collapse text-[14px]">
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri} className="border-b border-line">
                {r.map((c, ci) => (
                  <td key={ci} className={`px-2 py-1 ${ri === 0 ? "font-medium text-muted" : ""}`}>{inline(c)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>,
      );
    } else if (/^#{1,6} /.test(line)) {
      blocks.push(<div key={k++} className="mt-3 mb-1 font-semibold">{inline(line.replace(/^#+ /, ""))}</div>);
      i++;
    } else if (line.trim() === "") {
      i++;
    } else {
      const para: string[] = [];
      while (i < lines.length && lines[i].trim() !== "" && !/^(```|\s*[-*] |\s*\d+\. |\s*\||#{1,6} )/.test(lines[i])) para.push(lines[i++]);
      blocks.push(
        <p key={k++} className="my-1">
          {para.map((l, j) => (
            <Fragment key={j}>
              {inline(l)}
              {j < para.length - 1 && <br />}
            </Fragment>
          ))}
        </p>,
      );
    }
  }
  return <div className="leading-relaxed">{blocks}</div>;
}
