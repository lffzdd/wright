import { ArrowUp, Folder, GitBranch, House } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { useT } from "../../i18n";
import type { FolderListing } from "../../types";
import { shortPath } from "./session";

/** Pick a local folder and register it as a workspace. */
export function AddWorkspaceDialog({ close, register }: {
  close: () => void;
  register: (path: string) => Promise<void>;
}) {
  const tr = useT();
  const [listing, setListing] = useState<FolderListing | null>(null);
  const [path, setPath] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const request = useRef(0);
  const list = useRef<HTMLUListElement>(null);
  const browse = (target?: string) => {
    const id = ++request.current;
    setLoading(true);
    setError("");
    api.folders(target).then((next) => {
      if (id !== request.current) return;
      setListing(next);
      setPath(next.path);
      list.current?.scrollTo?.({ top: 0 });
    }).catch((reason) => {
      if (id === request.current) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (id === request.current) setLoading(false);
    });
  };
  useEffect(() => { browse(); }, []);
  const add = () => {
    const target = listing?.path;
    if (!target) return;
    setSaving(true);
    setError("");
    register(target).then(close).catch((reason) => {
      setError(reason instanceof Error ? reason.message : String(reason));
      setSaving(false);
    });
  };
  return <div className="small-dialog-backdrop" onPointerDown={(event) => { if (event.target === event.currentTarget) close(); }}>
    <section
      className="small-dialog folder-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="folder-dialog-title"
      onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); } }}
    >
      <h2 id="folder-dialog-title">{tr("web.register_workspace")}</h2>
      <p>{tr("web.folder.help")}</p>
      <form className="folder-bar" onSubmit={(event) => { event.preventDefault(); if (path.trim()) browse(path.trim()); }}>
        <button type="button" className="icon-button" title={tr("web.folder.up")} aria-label={tr("web.folder.up")} disabled={!listing?.parent || loading} onClick={() => listing?.parent && browse(listing.parent)}><ArrowUp size={14} /></button>
        <button type="button" className="icon-button" title={tr("web.folder.home")} aria-label={tr("web.folder.home")} disabled={loading} onClick={() => browse(listing?.home)}><House size={14} /></button>
        <input aria-label={tr("web.folder.path")} value={path} spellCheck={false} autoFocus onChange={(event) => setPath(event.target.value)} />
        <button type="submit" className="button" disabled={!path.trim() || loading}>{tr("web.folder.go")}</button>
      </form>
      <ul className="folder-list" ref={list} aria-busy={loading} aria-label={listing?.path ?? tr("web.folder.path")}>
        {loading && !listing && <li className="folder-empty">{tr("web.folder.loading")}</li>}
        {listing && !listing.entries.length && <li className="folder-empty">{tr("web.folder.empty")}</li>}
        {listing?.entries.map((entry) => <li key={entry.path}>
          <button type="button" className="folder-row" onClick={() => browse(entry.path)}>
            <Folder size={14} /><span className="folder-name">{entry.name}</span>{entry.git && <span className="folder-git"><GitBranch size={11} />git</span>}
          </button>
        </li>)}
        {listing?.truncated && <li className="folder-empty">{tr("web.folder.truncated", { count: listing.entries.length })}</li>}
      </ul>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="small-dialog-actions">
        {listing && <span className="folder-target" title={listing.path}>{listing.git && <GitBranch size={12} />}{shortPath(listing.path, 4)}</span>}
        <button type="button" className="button" onClick={close}>{tr("web.cancel")}</button>
        <button type="button" className="button primary" disabled={!listing || loading || saving} onClick={add}>{tr("web.folder.add")}</button>
      </div>
    </section>
  </div>;
}
