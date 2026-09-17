import { Check, Copy } from "@phosphor-icons/react";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { memo, useMemo, useState } from "react";

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

  return (
    <div className="code-block">
      <div className="code-header">
        <span className="code-lang">{lang || "code"}</span>
        <button
          type="button"
          className="copy-button"
          onClick={handleCopy}
          title="Copy code"
          aria-label="Copy code"
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
          <span>{copied ? "Copied" : "Copy"}</span>
        </button>
      </div>
      <pre><code>{code}</code></pre>
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
