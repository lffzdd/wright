import { Check, Copy } from "@phosphor-icons/react";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { memo, useMemo, useState, type CSSProperties } from "react";
import { languageForFence, useHighlight } from "./highlight";
import { useT } from "../../i18n";

type MarkdownChunk =
  | { type: "code"; lang?: string; text: string }
  | { type: "html"; html: string }
  | { type: "text"; text: string };

function sanitizeHtml(html: string): string {
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "iframe", "object", "embed", "form", "input", "button", "script"],
    FORBID_ATTR: ["style"],
  });
}

export function markdownChunks(content: string): MarkdownChunk[] {
  if (!content) return [];
  try {
    const tokens = marked.lexer(content);
    const result: MarkdownChunk[] = [];
    let buffer = "";

    const flush = () => {
      if (buffer) {
        result.push({
          type: "html",
          html: sanitizeHtml(marked.parse(buffer, { async: false, gfm: true, breaks: true }) as string),
        });
        buffer = "";
      }
    };

    for (const token of tokens) {
      if (token.type === "code") {
        flush();
        result.push({ type: "code", lang: token.lang, text: token.text });
      } else {
        buffer += token.raw;
      }
    }
    flush();
    return result;
  } catch {
    return [{ type: "text", text: content }];
  }
}

function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  const tr = useT();
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Ignore clipboard write failures
    }
  };

  const grammar = languageForFence(lang);
  const tokens = useHighlight(code, grammar);
  const label = lang?.trim().split(/\s+/)[0] || "";
  const copyLabel = copied ? tr("web.copied") : tr("web.copy_code");

  return (
    <div className="code-block">
      <div className="code-header">
        <span className="code-lang">{label}</span>
        <button
          type="button"
          className={`copy-button${copied ? " copied" : ""}`}
          onClick={handleCopy}
          title={copyLabel}
          aria-label={copyLabel}
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
        </button>
      </div>
      <pre><code>{tokens
        ? tokens.map((line, index) => <span key={index} className="code-line">
          {line.map((token, at) => <span key={at} className="cv-tok" style={token.style as CSSProperties}>{token.content}</span>)}
          {index < tokens.length - 1 ? "\n" : ""}
        </span>)
        : code}</code></pre>
    </div>
  );
}

export const MarkdownContent = memo(function MarkdownContent({ content }: { content: string }) {
  const chunks = useMemo(() => markdownChunks(content), [content]);

  if (!content) return null;

  return (
    <div className="markdown-body">
      {chunks.map((chunk, index) => {
        if (chunk.type === "code") {
          return <CodeBlock key={index} code={chunk.text} lang={chunk.lang} />;
        }
        if (chunk.type === "text") {
          return <p key={index} className="markdown-fallback">{chunk.text}</p>;
        }
        return (
          <div
            key={index}
            className="markdown-chunk"
            dangerouslySetInnerHTML={{ __html: chunk.html }}
          />
        );
      })}
    </div>
  );
});
