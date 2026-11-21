import { DotsThree, Plus, X } from "@phosphor-icons/react";
import { useState, type FormEvent } from "react";
import { useT } from "../../../i18n";
import type { McpServerForm, McpServerRow, McpTransport, SettingScope } from "../../../types";
import { confirmDialog } from "../../ui/dialogs";
import { MenuButton } from "../../ui/Menu";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { Tag } from "../capability";
import { Field } from "../shared";
import type { McpConfigState } from "./resources";

/** MCP servers in the user and project files. Connection status lives in each session's inspector. */
export function McpCard({ mcp, hasProject }: { mcp: McpConfigState; hasProject: boolean }) {
  const tr = useT();
  const [editing, setEditing] = useState<McpServerRow | "new" | null>(null);
  const view = mcp.data;
  if (!view) return mcp.error ? <p className="settings-problem settings-gated" role="alert">{mcp.error}</p> : null;
  const remove = async (server: McpServerRow) => {
    if (!await confirmDialog({ title: tr("web.mcp.remove_confirm", { name: server.name }), danger: true })) return;
    await mcp.remove(server.scope, server.name);
  };
  const fileErrors = Object.values(view.files).flatMap((file) => file?.error ? [file.error] : []);
  return <div className="mcp-card">
    {fileErrors.map((error) => <p key={error} className="settings-problem" role="alert">{error}</p>)}
    {view.servers.length > 0 && <ul className="mcp-list">
      {view.servers.map((server) => <li key={`${server.scope}-${server.name}`} className="mcp-server">
        <div className="mcp-server-main">
          <span className="mcp-server-name">{server.name}</span>
          <Tag>{server.transport}</Tag>
          <Tag tone={server.scope === "project" ? "accent" : "neutral"}>{tr(`web.permissions.scope.${server.scope}`)}</Tag>
          {!server.active && <Tag tone="warning">{tr("web.mcp.untrusted")}</Tag>}
          {server.shadowed && <Tag>{tr("web.mcp.shadowed")}</Tag>}
        </div>
        <code className="mcp-server-target">{server.url || [server.command, ...server.args].join(" ")}</code>
        {[...server.env, ...server.headers].length > 0 && <span className="hint">{tr("web.mcp.stored_values", { names: [...server.env, ...server.headers].join(", ") })}</span>}
        {server.problem && <p className="settings-problem">{server.problem}</p>}
        <div className="mcp-server-menu">
          <MenuButton label={tr("web.panel.actions_for", { name: server.name })} items={[
            { label: tr("web.mcp.edit"), onSelect: () => setEditing(server) },
            { label: tr("web.mcp.remove"), danger: true, onSelect: () => { remove(server).catch(() => undefined); } },
          ]}><DotsThree size={16} weight="bold" /></MenuButton>
        </div>
      </li>)}
    </ul>}
    {view.servers.length === 0 && !editing && <p className="hint settings-gated">{tr("web.mcp.none")}</p>}
    {editing
      ? <McpForm key={editing === "new" ? "new" : `${editing.scope}-${editing.name}`} server={editing === "new" ? null : editing}
        hasProject={hasProject} mcp={mcp} onDone={() => setEditing(null)} />
      : <div className="settings-card-foot">
        <button type="button" className="text-button entry-add" onClick={() => setEditing("new")}><Plus size={12} weight="bold" />{tr("web.mcp.add")}</button>
        <span className="hint">{tr("web.mcp.status_hint")}</span>
      </div>}
  </div>;
}

type Pair = { key: string; value: string; stored: boolean };

function pairsOf(names: string[]): Pair[] {
  return names.map((key) => ({ key, value: "", stored: true }));
}

/** Pairs back to the request: a stored value left blank is kept (null). */
function recordOf(pairs: Pair[]): Record<string, string | null> {
  const record: Record<string, string | null> = {};
  for (const pair of pairs) {
    const key = pair.key.trim();
    if (!key) continue;
    record[key] = pair.value === "" && pair.stored ? null : pair.value;
  }
  return record;
}

function storedNames(server: McpServerRow | null): string[] {
  if (!server) return [];
  return server.transport === "stdio" ? server.env : server.headers;
}

function McpForm({ server, hasProject, mcp, onDone }: {
  server: McpServerRow | null;
  hasProject: boolean;
  mcp: McpConfigState;
  onDone: () => void;
}) {
  const tr = useT();
  const [name, setName] = useState(server?.name ?? "");
  const [scope, setScope] = useState<SettingScope>(server?.scope ?? "user");
  const [transport, setTransport] = useState<McpTransport>(server && server.transport !== "unknown" ? server.transport : "stdio");
  const [target, setTarget] = useState(server ? (server.url || [server.command, ...server.args].join("\n")) : "");
  const [pairs, setPairs] = useState<Pair[]>(pairsOf(storedNames(server)));
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const lines = target.split("\n").map((line) => line.trim()).filter(Boolean);
    const form: McpServerForm = transport === "stdio"
      ? { scope, transport, command: lines[0] ?? "", args: lines.slice(1), env: recordOf(pairs) }
      : { scope, transport, url: lines[0] ?? "", headers: recordOf(pairs) };
    if (server && server.name !== name.trim()) form.previous = server.name;
    if (await mcp.save(name.trim(), form)) onDone();
  };
  const setPair = (index: number, patch: Partial<Pair>) => setPairs((current) => current.map(
    (pair, at) => at === index ? { ...pair, ...patch } : pair,
  ));
  return <form className="cap-form mcp-form" onSubmit={(event) => { submit(event).catch(() => undefined); }}>
    <Field label={tr("web.mcp.name")}><input value={name} spellCheck={false} onChange={(event) => setName(event.target.value)} autoFocus /></Field>
    {hasProject && !server && <div className="dialog-field"><span>{tr("web.mcp.file")}</span>
      <SegmentedControl<SettingScope> ariaLabel={tr("web.mcp.file")} value={scope} onChange={setScope}
        options={[{ value: "user", label: tr("web.permissions.scope.user") }, { value: "project", label: tr("web.permissions.scope.project") }]} />
    </div>}
    <div className="dialog-field"><span>{tr("web.mcp.transport")}</span>
      <SegmentedControl<McpTransport> ariaLabel={tr("web.mcp.transport")} value={transport} onChange={setTransport}
        options={[{ value: "stdio", label: "stdio" }, { value: "http", label: "HTTP" }, { value: "sse", label: "SSE" }]} />
    </div>
    <Field label={tr(transport === "stdio" ? "web.mcp.command" : "web.mcp.url")}>
      {transport === "stdio"
        ? <textarea className="mono" rows={3} value={target} spellCheck={false} placeholder={tr("web.mcp.command_placeholder")}
          onChange={(event) => setTarget(event.target.value)} />
        : <input className="mono-input" value={target} spellCheck={false} placeholder="https://" onChange={(event) => setTarget(event.target.value)} />}
    </Field>
    <div className="dialog-field"><span>{tr(transport === "stdio" ? "web.mcp.env" : "web.mcp.headers")}</span>
      <div className="mcp-pairs">
        {pairs.map((pair, index) => <div key={index} className="mcp-pair">
          <input className="mono-input" aria-label={tr("web.mcp.pair_name")} value={pair.key} spellCheck={false}
            onChange={(event) => setPair(index, { key: event.target.value, stored: false })} />
          <input className="mono-input" type="password" autoComplete="off" aria-label={tr("web.mcp.pair_value", { name: pair.key })}
            placeholder={pair.stored ? tr("web.mcp.keep_value") : ""} value={pair.value} onChange={(event) => setPair(index, { value: event.target.value })} />
          <button type="button" className="icon-button" aria-label={tr("web.entries.remove", { row: index + 1 })}
            onClick={() => setPairs((current) => current.filter((_, at) => at !== index))}><X size={12} weight="bold" /></button>
        </div>)}
        <button type="button" className="text-button entry-add" onClick={() => setPairs((current) => [...current, { key: "", value: "", stored: false }])}>
          <Plus size={12} weight="bold" />{tr("web.mcp.add_pair")}
        </button>
      </div>
    </div>
    {scope === "project" && <p className="hint">{tr("web.mcp.project_note")}</p>}
    <div className="dialog-actions">
      <button type="button" className="button" onClick={onDone}>{tr("web.cancel")}</button>
      <button type="submit" className="button primary" disabled={!name.trim() || !target.trim()}>{tr("web.mcp.save")}</button>
    </div>
  </form>;
}
