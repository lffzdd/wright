import { useEffect, useState } from "react";
import type { HighlighterCore } from "shiki/core";

/** One colored run of text; `style` carries both theme colors as CSS variables. */
export type Token = { content: string; style?: Record<string, string> };

// Grammars load on first use, so a session that never shows Rust never fetches it.
const GRAMMARS: Record<string, () => Promise<unknown>> = {
  python: () => import("shiki/langs/python.mjs"),
  typescript: () => import("shiki/langs/typescript.mjs"),
  tsx: () => import("shiki/langs/tsx.mjs"),
  javascript: () => import("shiki/langs/javascript.mjs"),
  jsx: () => import("shiki/langs/jsx.mjs"),
  json: () => import("shiki/langs/json.mjs"),
  shellscript: () => import("shiki/langs/shellscript.mjs"),
  markdown: () => import("shiki/langs/markdown.mjs"),
  yaml: () => import("shiki/langs/yaml.mjs"),
  toml: () => import("shiki/langs/toml.mjs"),
  css: () => import("shiki/langs/css.mjs"),
  html: () => import("shiki/langs/html.mjs"),
  rust: () => import("shiki/langs/rust.mjs"),
  go: () => import("shiki/langs/go.mjs"),
  sql: () => import("shiki/langs/sql.mjs"),
  diff: () => import("shiki/langs/diff.mjs"),
  dockerfile: () => import("shiki/langs/dockerfile.mjs"),
  ini: () => import("shiki/langs/ini.mjs"),
};

const EXTENSIONS: Record<string, string> = {
  py: "python", pyi: "python",
  ts: "typescript", mts: "typescript", cts: "typescript", tsx: "tsx",
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "jsx",
  json: "json", jsonc: "json", jsonl: "json",
  sh: "shellscript", bash: "shellscript", zsh: "shellscript",
  md: "markdown", markdown: "markdown",
  yml: "yaml", yaml: "yaml", toml: "toml",
  css: "css", html: "html", htm: "html",
  rs: "rust", go: "go", sql: "sql",
  diff: "diff", patch: "diff",
  ini: "ini", cfg: "ini", conf: "ini",
};

const NAMES: Record<string, string> = { dockerfile: "dockerfile", ".env": "ini", ".env.example": "ini", makefile: "shellscript" };

/** The grammar for a file path, if one is bundled. */
export function languageForPath(path: string | undefined): string | undefined {
  if (!path) return undefined;
  const name = path.replace(/\/+$/, "").split("/").pop()?.toLowerCase() ?? "";
  if (NAMES[name]) return NAMES[name];
  const dot = name.lastIndexOf(".");
  return dot > 0 ? EXTENSIONS[name.slice(dot + 1)] : undefined;
}

/** A short label for the language, shown next to a file. */
export function languageLabel(lang: string | undefined): string {
  if (!lang) return "";
  return ({ shellscript: "shell", typescript: "TypeScript", javascript: "JavaScript", tsx: "TSX", jsx: "JSX", python: "Python" } as Record<string, string>)[lang] ?? lang.toUpperCase();
}

// Unit tests render plain text; tokenizing there only adds async noise.
const IN_TESTS = typeof navigator !== "undefined" && /jsdom/i.test(navigator.userAgent);

// Past this, tokenizing would stall the page for little benefit.
const MAX_CHARS = 200_000;

let core: Promise<HighlighterCore> | null = null;
const grammars = new Map<string, Promise<void>>();

function highlighter(): Promise<HighlighterCore> {
  core ??= (async () => {
    const [{ createHighlighterCore }, { createJavaScriptRegexEngine }] = await Promise.all([
      import("shiki/core"), import("shiki/engine/javascript"),
    ]);
    return createHighlighterCore({
      themes: [import("shiki/themes/github-light.mjs"), import("shiki/themes/github-dark.mjs")],
      langs: [],
      engine: createJavaScriptRegexEngine({ forgiving: true }),
    });
  })();
  return core;
}

async function tokenize(code: string, lang: string): Promise<Token[][] | null> {
  const grammar = GRAMMARS[lang];
  if (!grammar) return null;
  const hl = await highlighter();
  if (!grammars.has(lang)) grammars.set(lang, grammar().then((module) => hl.loadLanguage((module as { default: Parameters<HighlighterCore["loadLanguage"]>[0] }).default)));
  await grammars.get(lang);
  const { tokens } = hl.codeToTokens(code, { lang, themes: { light: "github-light", dark: "github-dark" }, defaultColor: false });
  return tokens.map((line) => line.map((token) => ({ content: token.content, style: token.htmlStyle as Record<string, string> | undefined })));
}

/**
 * Syntax colors for `code`, one token list per line, or null until they are
 * ready (and for unknown languages). Callers render plain text meanwhile.
 */
export function useHighlight(code: string, lang: string | undefined): Token[][] | null {
  const [state, setState] = useState<{ code: string; lang: string; lines: Token[][] } | null>(null);
  useEffect(() => {
    if (!lang || !GRAMMARS[lang] || !code || code.length > MAX_CHARS || IN_TESTS) return;
    let live = true;
    tokenize(code, lang).then((lines) => { if (live && lines) setState({ code, lang, lines }); }).catch(() => undefined);
    return () => { live = false; };
  }, [code, lang]);
  return state && state.code === code && state.lang === lang ? state.lines : null;
}
