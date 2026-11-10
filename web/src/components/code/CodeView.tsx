import { useEffect, useRef, type CSSProperties, type ReactNode } from "react";
import { useHighlight } from "./highlight";

export type CodeLine = {
  /** Line number shown in the gutter. */
  num?: number;
  text: string;
  /** Replaces the syntax-colored text, e.g. with search marks. */
  content?: ReactNode;
};

/**
 * Source lines with a line-number gutter and syntax colors.
 *
 * `card` caps its height for the timeline; `panel` grows with its container.
 * Long lines scroll sideways unless `wrap` is set; the gutter stays put.
 */
export function CodeView({ lines, lang, variant = "card", wrap = false, focusLine, onLine, label }: {
  lines: CodeLine[];
  lang?: string;
  variant?: "card" | "panel";
  wrap?: boolean;
  focusLine?: number;
  onLine?: (num: number) => void;
  label?: string;
}) {
  const code = lines.map((line) => line.text).join("\n");
  const tokens = useHighlight(code, lang);
  const box = useRef<HTMLDivElement>(null);
  const focused = useRef<HTMLDivElement>(null);
  // Scroll only this box; scrollIntoView would also move the page around it.
  useEffect(() => {
    const row = focused.current, container = box.current;
    if (row && container) container.scrollTop = row.offsetTop - (container.clientHeight - row.offsetHeight) / 2;
  }, [focusLine, lines.length]);
  const gutter = lines.some((line) => line.num !== undefined);
  const digits = Math.max(2, ...lines.map((line) => String(line.num ?? "").length));
  return <div ref={box} className={`cv cv-${variant}${wrap ? " cv-wrap" : ""}`} style={{ "--cv-digits": digits } as CSSProperties} role="region" aria-label={label} tabIndex={0}>
    <div className="cv-body">
      {lines.map((line, index) => {
        const focus = focusLine !== undefined && line.num === focusLine;
        const clickable = onLine && line.num !== undefined;
        return <div key={index} ref={focus ? focused : undefined} className={`cv-row${focus ? " cv-focus" : ""}${clickable ? " cv-link" : ""}`}
          onClick={clickable ? () => onLine(line.num!) : undefined}>
          {gutter && <span className="cv-num" aria-hidden="true">{line.num ?? ""}</span>}
          <span className="cv-src">{line.content ?? (tokens?.[index]?.length
            ? tokens[index].map((token, at) => <span key={at} className="cv-tok" style={token.style as CSSProperties}>{token.content}</span>)
            : line.text || " ")}</span>
        </div>;
      })}
    </div>
  </div>;
}
