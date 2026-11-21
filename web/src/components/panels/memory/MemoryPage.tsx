import { useCallback, useEffect, useState } from "react";
import { Brain, MagnifyingGlass, Plus, X } from "@phosphor-icons/react";
import { CapabilityPage, EmptyState, Tabs } from "../capability";
import { PageNotice, noteOf, type Notice } from "../shared";
import { api } from "../../../api";
import { useT } from "../../../i18n";
import { ChangesView } from "./ChangesView";
import { CommitmentsView } from "./CommitmentsView";
import { EpisodesView } from "./EpisodesView";
import { FactsView } from "./FactsView";
import type { Change, Commitment, Episode, Fact, MemorySnapshot, RunAction } from "./types";

type Raw = Record<string, unknown>;
const list = (value: unknown): Raw[] => (Array.isArray(value) ? value.filter((item): item is Raw => Boolean(item) && typeof item === "object") : []);
const text = (value: unknown): string => (typeof value === "string" ? value : value === undefined || value === null ? "" : String(value));
const strings = (value: unknown): string[] => (Array.isArray(value) ? value.map(text).filter(Boolean) : []);

/** Fill gaps so a partial or older server response still renders. */
function normalize(raw: Raw): MemorySnapshot {
  return {
    project_id: text(raw.project_id),
    current_space: text(raw.current_space),
    spaces: list(raw.spaces).map((item) => ({ id: text(item.id), type: text(item.type) as MemorySnapshot["spaces"][number]["type"], name: text(item.name) })),
    facts: list(raw.facts).map((item) => ({
      ...item,
      id: text(item.id), space_id: text(item.space_id), kind: (text(item.kind) || "fact") as Fact["kind"], subject: text(item.subject),
      content: text(item.content), why: text(item.why), origin: (text(item.origin) || "agent_inferred") as Fact["origin"],
      sensitivity: (text(item.sensitivity) || "none") as Fact["sensitivity"], tier: (text(item.tier) || "archived") as Fact["tier"],
      status: (text(item.status) || "active") as Fact["status"], markers: strings(item.markers), sources: strings(item.sources),
      created_at: text(item.created_at), updated_at: text(item.updated_at || item.created_at), valid_to: text(item.valid_to),
      superseded_by: text(item.superseded_by), last_used_at: text(item.last_used_at), use_count: Number(item.use_count) || 0,
      revision: Number(item.revision) || 1, shareable: Boolean(item.shareable),
    })),
    episodes: list(raw.episodes).map((item): Episode => {
      const links = (item.links && typeof item.links === "object" ? item.links : {}) as Raw;
      return {
        id: text(item.id), goal: text(item.goal), status: text(item.status), outcome: text(item.outcome), created_at: text(item.created_at),
        links: { commits: strings(links.commits), prs: strings(links.prs) }, compacted: Boolean(item.compacted),
      };
    }),
    commitments: list(raw.commitments).map((item): Commitment => ({
      id: text(item.id), space_id: text(item.space_id), content: text(item.content), status: (text(item.status) || "open") as Commitment["status"],
      due: text(item.due), created_at: text(item.created_at), updated_at: text(item.updated_at || item.created_at),
    })),
    changes: list(raw.changes).map((item): Change => ({
      id: Number(item.id), at: text(item.at), actor: (text(item.actor) || "agent") as Change["actor"], action: text(item.action),
      target_id: text(item.target_id), target_type: (text(item.target_type) || "other") as Change["target_type"], scope: text(item.scope),
      summary: text(item.summary), fields: list(item.fields).map((field) => ({ name: text(field.name), before: field.before, after: field.after })),
      note: text(item.note), reverted: Boolean(item.reverted), forgotten: Boolean(item.forgotten), undoable: Boolean(item.undoable),
    })),
  };
}

type MemoryTab = "facts" | "episodes" | "commitments" | "changes";

export function MemoryPage({ projectId }: { projectId: string | null }) {
  const tr = useT();
  const [data, setData] = useState<MemorySnapshot | null>(null);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [tab, setTab] = useState<MemoryTab>("facts");
  const [query, setQuery] = useState("");
  const [composing, setComposing] = useState(false);

  const load = useCallback(async (quiet = false) => {
    if (!projectId) return;
    if (!quiet) setNotice({ tone: "busy", text: tr("web.panel.loading") });
    try {
      setData(normalize(await api.memory(projectId)));
      setNotice({ tone: "idle", text: "" });
    } catch (error) { setNotice(noteOf(error)); }
  }, [projectId, tr]);
  useEffect(() => { setData(null); setComposing(false); setQuery(""); load().catch(() => undefined); }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps

  const run: RunAction = async (action) => {
    setNotice({ tone: "busy", text: tr("web.saving") });
    try {
      await action();
      await load(true);
      return true;
    } catch (error) {
      setNotice(noteOf(error));
      return false;
    }
  };

  const switchTab = (next: MemoryTab) => { setTab(next); setComposing(false); };
  const facts = data?.facts ?? [];
  const pending = facts.filter((item) => item.status === "quarantined").length;
  const canCompose = tab === "facts" || tab === "commitments";

  const actions = projectId && <>
    <label className="mem-search">
      <MagnifyingGlass size={14} aria-hidden="true" />
      <input type="search" aria-label={tr("web.memory.search")} placeholder={tr("web.memory.search")} value={query} onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => { if (event.key === "Escape") setQuery(""); }} />
      {query && <button type="button" className="mem-search-clear" aria-label={tr("web.memory.search_clear")} onClick={() => setQuery("")}><X size={12} /></button>}
    </label>
    {canCompose && <button type="button" className="button primary" disabled={composing} onClick={() => setComposing(true)}>
      <Plus size={12} weight="bold" />{tab === "facts" ? tr("web.memory.new_fact") : tr("web.memory.new_commitment")}
    </button>}
  </>;

  return <CapabilityPage id="memory-title" title={tr("web.panel.memory")} subtitle={tr("web.memory.subtitle")} actions={actions || undefined}
    notice={<PageNotice notice={notice} />}>
    {!projectId ? <EmptyState icon={<Brain size={22} />} title={tr("web.panel.no_project")}>{tr("web.panel.needs_workspace")}</EmptyState> : <>
      <Tabs label={tr("web.panel.memory")} value={tab} onChange={switchTab} tabs={[
        { value: "facts", label: pending ? tr("web.memory.tab.facts_pending", { count: pending }) : tr("web.memory.tab.facts"), count: data ? facts.filter((item) => item.status === "active").length : undefined },
        { value: "episodes", label: tr("web.memory.tab.episodes"), count: data ? data.episodes.length : undefined },
        { value: "commitments", label: tr("web.memory.tab.commitments"), count: data ? data.commitments.filter((item) => item.status === "open").length : undefined },
        { value: "changes", label: tr("web.memory.tab.changes") },
      ]} />
      {data && tab === "facts" && <FactsView projectId={projectId} facts={facts} spaces={data.spaces} query={query} composing={composing} onCloseComposer={() => setComposing(false)} run={run} />}
      {data && tab === "episodes" && <EpisodesView projectId={projectId} episodes={data.episodes} query={query} run={run} />}
      {data && tab === "commitments" && <CommitmentsView projectId={projectId} commitments={data.commitments} query={query} composing={composing} onCloseComposer={() => setComposing(false)} run={run} />}
      {data && tab === "changes" && <ChangesView projectId={projectId} changes={data.changes} query={query} run={run} />}
    </>}
  </CapabilityPage>;
}
