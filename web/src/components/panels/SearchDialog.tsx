import { useEffect, useRef, useState } from "react";
import { MagnifyingGlass } from "@phosphor-icons/react";
import { Select } from "../ui/Select";
import { api } from "../../api";
import { useT } from "../../i18n";
import { PanelNotice, noteOf, type Notice } from "./shared";

export function SearchDialog({
  sessionId, projectId, close, openFile, openSession, previewFiles = false,
}: {
  sessionId: string | null;
  projectId: string | null;
  close: () => void;
  openFile: (path: string) => void;
  openSession?: (sessionId: string) => void | Promise<void>;
  previewFiles?: boolean;
}) {
  const tr = useT();
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("file");
  const [results, setResults] = useState<Array<Record<string, unknown>>>([]);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [preview, setPreview] = useState<{ path: string; content: string } | null>(null);
  const [shown, setShown] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setShown(true));
    return () => window.cancelAnimationFrame(frame);
  }, []);
  const typing = useRef<number | undefined>(undefined);
  useEffect(() => () => { generation.current += 1; }, []);
  const run = async () => {
    window.clearTimeout(typing.current);
    if (!query.trim()) { setNotice({ tone: "error", text: tr("web.panel.search_empty") }); return; }
    setNotice({ tone: "busy", text: tr("web.panel.searching") });
    const request = ++generation.current;
    setPreview(null);
    try {
      const found = sessionId
        ? await api.search(sessionId, query.trim(), kind)
        : projectId
          ? await api.workspaceSearch(projectId, query.trim(), kind)
          : null;
      if (request !== generation.current) return;
      if (!found) { setNotice({ tone: "error", text: tr("web.panel.search_no_workspace") }); return; }
      setResults(found.results);
      setNotice({ tone: "ok", text: found.results.length ? tr(found.results.length === 1 ? "web.panel.results_one" : "web.panel.results_many", { count: found.results.length }) : tr("web.panel.no_matches") });
    } catch (error) { if (request === generation.current) setNotice(noteOf(error)); }
  };
  useEffect(() => {
    ++generation.current;
    setResults([]); setPreview(null); setNotice({ tone: "idle", text: "" });
    if (!query.trim()) return;
    typing.current = window.setTimeout(() => { run().catch(() => undefined); }, 250);
    return () => window.clearTimeout(typing.current);
  }, [sessionId, projectId, query, kind]);
  const activate = async (item: Record<string, unknown>) => {
    if (item.kind === "task" && item.session_id && openSession) {
      try { await openSession(String(item.session_id)); }
      catch (error) { setNotice(noteOf(error)); }
      return;
    }
    if (!item.path) return;
    const path = String(item.path);
    if (!previewFiles) { openFile(path); return; }
    if (!projectId) return;
    const request = ++generation.current;
    setPreview(null);
    setNotice({ tone: "busy", text: tr("web.panel.loading_file") });
    try {
      const file = await api.workspaceFile(projectId, path);
      if (request !== generation.current) return;
      setPreview({ path, content: file.binary ? tr("web.panel.binary_file") : file.content });
      setNotice({ tone: "ok", text: "" });
    } catch (error) { if (request === generation.current) setNotice(noteOf(error)); }
  };
  return <div className={`overlay palette-overlay${shown ? " show" : ""}`} role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
    <div className="palette" role="dialog" aria-modal="true" aria-labelledby="search-title">
      <div className="pal-in">
        <MagnifyingGlass size={16} aria-hidden="true" />
        <input autoFocus id="search-title" aria-label={tr("web.search")} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); close(); } if (event.key === "Enter" && !event.nativeEvent.isComposing) run(); }} placeholder={tr("web.search")} />
        <Select className="field-select" ariaLabel="Search kind" value={kind} onChange={setKind}
          options={["file", "content", "task", "symbol"].map((value) => ({ value, label: tr(`web.panel.kind_${value}`) }))} />
      </div>
      <PanelNotice notice={notice} />
      <div className="pal-list change-list">{results.length ? results.map((item, index) => {
        const path = String(item.path ?? item.name ?? item.title ?? item.id ?? index);
        const line = item.line ? `:${item.line}` : "";
        return <button type="button" className={`pal-i${index === 0 ? " sel" : ""}`} key={`${path}-${index}`} onClick={() => activate(item)}>
          <span className="lbl">{item.kind ? `${item.kind} ` : ""}{path}{line}</span>
          {typeof item.text === "string" && <span className="d">{item.text.trim().slice(0, 80)}</span>}
        </button>;
      }) : <p className="pal-empty">{query.trim() ? tr("web.panel.no_matches") : tr("web.panel.search_empty")}</p>}</div>
      <div className="pal-foot">
        <span><kbd>↵</kbd>{tr("web.panel.search")}</span>
        <span><kbd>Esc</kbd>{tr("web.close")}</span>
      </div>
      {preview && <section className="palette-preview" aria-label="File preview"><h3>{preview.path}</h3><pre className="term-body">{preview.content || tr("web.empty_file")}</pre></section>}
    </div>
  </div>;
}
