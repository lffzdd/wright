import { useEffect, useState } from "react";
import { Clock, Plus } from "@phosphor-icons/react";
import { confirmDialog } from "../ui/dialogs";
import { Select } from "../ui/Select";
import { shortTime } from "../inspector/Inspector";
import { CapabilityPage, EmptyState, Row, Switch, Tag } from "./capability";
import { INTERVAL_UNITS, describeInterval, splitInterval, toSeconds, type IntervalUnit } from "./interval";
import { Field, PageNotice, noteOf, preview, stamp, statusLabel, type Notice } from "./shared";
import { api } from "../../api";
import { useT } from "../../i18n";

function runTone(status: string): "ok" | "danger" | "accent" | "neutral" {
  if (status === "completed") return "ok";
  if (status === "failed") return "danger";
  if (status === "running" || status === "dispatched" || status === "queued" || status === "waiting_retry") return "accent";
  return "neutral";
}

export function SchedulesPage({ sessionId, projectId }: { sessionId: string | null; projectId: string | null }) {
  const tr = useT();
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([]);
  const [history, setHistory] = useState<{ id: string; runs: Array<Record<string, unknown>> } | null>(null);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [formOpen, setFormOpen] = useState(false);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [count, setCount] = useState("1");
  const [unit, setUnit] = useState<IntervalUnit>("hours");
  const [editing, setEditing] = useState<string | null>(null);
  const load = async () => {
    if (!projectId) return;
    setNotice({ tone: "busy", text: tr("web.panel.loading") });
    try { setRows(await api.projectSchedules(projectId)); setNotice({ tone: "idle", text: "" }); }
    catch (error) { setNotice(noteOf(error)); }
  };
  useEffect(() => { setRows([]); setHistory(null); setFormOpen(false); load().catch(() => undefined); }, [projectId]);
  const act = async (id: string, action: string) => {
    if (!projectId) return;
    if (action === "delete" && !await confirmDialog({ title: tr("web.panel.delete_schedule_confirm", { name: id }), danger: true })) return;
    setNotice({ tone: "busy", text: tr("web.saving") });
    try { await api.scheduleAction(projectId, id, action, action === "delete" ? { confirm: true } : {}); await load(); }
    catch (error) { setNotice(noteOf(error)); }
  };
  const openForm = (row?: Record<string, unknown>) => {
    const trigger = row?.trigger as { every_seconds?: number } | undefined;
    const split = splitInterval(trigger?.every_seconds ?? 3600);
    setEditing(row ? String(row.id) : null);
    setName(row ? String(row.name ?? "") : "");
    setPrompt(row ? String(row.prompt ?? "") : "");
    setCount(String(split.count)); setUnit(split.unit);
    setFormOpen(true);
  };
  const showHistory = async (id: string) => {
    if (history?.id === id) { setHistory(null); return; }
    if (!sessionId) { setNotice({ tone: "error", text: tr("web.panel.history_needs_task") }); return; }
    try { setHistory({ id, runs: await api.scheduleRuns(sessionId, id) }); }
    catch (error) { setNotice(noteOf(error)); }
  };
  const save = async () => {
    const amount = Number(count);
    if (!name.trim() || !prompt.trim() || !Number.isFinite(amount) || amount <= 0) { setNotice({ tone: "error", text: tr("web.panel.schedule_required") }); return; }
    const trigger = { type: "interval", every_seconds: toSeconds(amount, unit) };
    setNotice({ tone: "busy", text: tr("web.saving") });
    try {
      if (editing) {
        if (!projectId) return;
        await api.scheduleAction(projectId, editing, "update", { name, prompt, trigger });
      } else {
        if (!sessionId) { setNotice({ tone: "error", text: tr("web.panel.schedule_needs_task") }); return; }
        await api.createSchedule(sessionId, { name, prompt, trigger, recovery_policy: "manual" });
      }
      setEditing(null); setFormOpen(false); setName(""); setPrompt(""); await load();
    } catch (error) { setNotice(noteOf(error)); }
  };
  return <CapabilityPage id="schedule-title" title={tr("web.panel.schedules")}
    subtitle={projectId && !sessionId ? tr("web.panel.schedule_needs_task") : undefined}
    actions={projectId && !formOpen ? <button type="button" className="button primary" disabled={!sessionId} onClick={() => openForm()}><Plus size={12} weight="bold" />{tr("web.panel.new_schedule")}</button> : undefined}
    notice={<PageNotice notice={notice} />}>
    {!projectId ? <EmptyState icon={<Clock size={22} />} title={tr("web.panel.no_project")} /> : <div className="cap-stack">
      {formOpen && <div className="cap-form">
        <h3>{tr(editing ? "web.panel.edit_schedule" : "web.panel.add_schedule")}</h3>
        <Field label={tr("web.panel.name")}><input aria-label="Schedule name" value={name} onChange={(event) => setName(event.target.value)} autoFocus /></Field>
        <Field label={tr("web.panel.prompt")}><textarea aria-label="Schedule prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={4} /></Field>
        <div className="dialog-field"><span>{tr("web.panel.run_every")}</span>
          <div className="interval-field">
            <input aria-label={tr("web.panel.run_every")} type="number" min={1} value={count} onChange={(event) => setCount(event.target.value)} />
            <Select className="field-select" ariaLabel={tr("web.panel.interval_unit")} value={unit} onChange={(next) => setUnit(next as IntervalUnit)}
              options={INTERVAL_UNITS.map((value) => ({ value, label: tr(`web.panel.unit.${value}`) }))} />
          </div>
        </div>
        <div className="dialog-actions">
          <button type="button" className="button" onClick={() => { setFormOpen(false); setEditing(null); }}>{tr("web.cancel")}</button>
          <button type="button" className="button primary" onClick={() => { save().catch(() => undefined); }}>{tr(editing ? "web.panel.save_schedule" : "web.panel.create_schedule")}</button>
        </div>
      </div>}
      {rows.length ? <ul className="cap-list">{rows.map((row) => {
        const id = String(row.id);
        const title = String(row.name ?? row.id);
        const status = String(row.status ?? "");
        const trigger = row.trigger as { type?: string; every_seconds?: number } | undefined;
        const next = stamp(row.next_run_at);
        const toggleable = status === "active" || status === "paused";
        return <Row key={id} title={title} preview={preview(row.prompt)}
          meta={<>
            <Tag>{trigger?.every_seconds ? describeInterval(trigger.every_seconds, tr) : tr("web.panel.trigger_other", { type: String(trigger?.type ?? "?") })}</Tag>
            {status && <Tag tone={status === "active" ? "ok" : status === "paused" ? "warning" : "neutral"}>{tr(`web.panel.schedule_status.${status}`)}</Tag>}
            {next && status === "active" && <span className="cap-row-time">{tr("web.panel.next_run", { time: shortTime(next) })}</span>}
          </>}
          side={<Switch label={tr("web.panel.toggle_schedule", { name: title })} checked={status === "active"} disabled={!toggleable} onChange={(on) => { act(id, on ? "resume" : "pause").catch(() => undefined); }} />}
          menu={[
            { label: tr("web.panel.edit"), onSelect: () => openForm(row) },
            { label: tr(history?.id === id ? "web.panel.hide_history" : "web.panel.history"), onSelect: () => { showHistory(id).catch(() => undefined); } },
            "separator",
            { label: tr("web.panel.delete"), danger: true, onSelect: () => { act(id, "delete").catch(() => undefined); } },
          ]}>
          {history?.id === id && <ul className="run-list" aria-label={tr("web.panel.history")}>
            {history.runs.length ? history.runs.map((run) => {
              const runStatus = String(run.status ?? "");
              const at = stamp(run.started_at) ?? stamp(run.created_at);
              return <li key={String(run.id ?? run.run_id)}><Tag tone={runTone(runStatus)}>{statusLabel(tr, runStatus)}</Tag>{at && <span className="cap-row-time">{shortTime(at)}</span>}</li>;
            }) : <li className="empty-small">{tr("web.panel.no_runs")}</li>}
          </ul>}
        </Row>;
      })}</ul> : !formOpen && notice.tone !== "busy" && <EmptyState icon={<Clock size={22} />} title={tr("web.panel.no_schedules")}>{tr("web.panel.schedules_hint")}</EmptyState>}
    </div>}
  </CapabilityPage>;
}
