import { useState } from "react";
import { DotsThree, GitCommit, GitPullRequest } from "@phosphor-icons/react";
import { confirmDialog } from "../../ui/dialogs";
import { MenuButton } from "../../ui/Menu";
import { api } from "../../../api";
import { useT } from "../../../i18n";
import { statusLabel } from "../shared";
import { dayHeading, linkLabel, matches, relativeTime } from "./labels";
import type { Episode, RunAction } from "./types";

export function EpisodesView({ projectId, episodes, query, run }: { projectId: string; episodes: Episode[]; query: string; run: RunAction }) {
  const tr = useT();
  const visible = episodes.filter((item) => matches(query, item.goal, item.outcome));
  if (!visible.length) {
    return <div className="mem-view" role="tabpanel"><div className="mem-empty">
      <p className="mem-empty-title">{query.trim() ? tr("web.memory.empty.no_match") : tr("web.memory.empty.episodes")}</p>
      {!query.trim() && <p className="hint">{tr("web.memory.empty.episodes_hint")}</p>}
    </div></div>;
  }
  const groups: Array<{ day: string; items: Episode[] }> = [];
  for (const item of visible) {
    const day = dayHeading(tr, item.created_at);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.items.push(item);
    else groups.push({ day, items: [item] });
  }
  return <div className="mem-view" role="tabpanel">
    <p className="mem-lede">{tr("web.memory.episodes.lede")}</p>
    {groups.map((group) => <section key={group.day} className="mem-group">
      <header className="mem-group-head"><h2>{group.day}</h2></header>
      <ol className="mem-timeline">
        {group.items.map((item) => <EpisodeItem key={item.id} projectId={projectId} episode={item} run={run} />)}
      </ol>
    </section>)}
  </div>;
}

function EpisodeItem({ projectId, episode, run }: { projectId: string; episode: Episode; run: RunAction }) {
  const tr = useT();
  const [open, setOpen] = useState(false);
  const title = episode.goal || episode.id;
  const tone = episode.status === "completed" ? "ok" : episode.status === "failed" ? "danger" : "neutral";
  const commits = episode.links?.commits ?? [];
  const prs = episode.links?.prs ?? [];
  const forget = async () => {
    if (!await confirmDialog({ title: tr("web.memory.episode.forget_confirm"), message: title, confirmLabel: tr("web.memory.fact.forget"), danger: true })) return;
    await run(() => api.forgetMemory(projectId, episode.id));
  };
  return <li className={`mem-episode${open ? " open" : ""}`}>
    <span className={`mem-dot ${tone}`} aria-hidden="true" />
    <div className="mem-episode-main">
      <div className="mem-item-head">
        <button type="button" className="mem-item-body" aria-expanded={open} onClick={() => setOpen(!open)}>
          <p className="mem-item-text">{title}</p>
          {episode.outcome && <p className={`mem-episode-outcome${open ? " full" : ""}`}>{episode.outcome}</p>}
          <div className="mem-item-meta">
            <span className={`mem-status ${tone}`}>{statusLabel(tr, episode.status)}</span>
            <span className="mem-dim">{relativeTime(tr, episode.created_at)}</span>
            {episode.compacted && <span className="mem-dim">{tr("web.memory.episode.compacted")}</span>}
          </div>
        </button>
        <div className="mem-item-actions">
          <MenuButton label={tr("web.panel.actions_for", { name: title.slice(0, 40) })}
            items={[{ label: tr("web.memory.fact.forget"), danger: true, onSelect: () => { forget().catch(() => undefined); } }]}>
            <DotsThree size={16} weight="bold" />
          </MenuButton>
        </div>
      </div>
      {(commits.length > 0 || prs.length > 0) && <div className="mem-links">
        {prs.map((url) => <a key={url} href={url} target="_blank" rel="noreferrer" className="mem-link"><GitPullRequest size={12} />{linkLabel("prs", url)}</a>)}
        {commits.map((sha) => <span key={sha} className="mem-link" title={sha}><GitCommit size={12} />{linkLabel("commits", sha)}</span>)}
      </div>}
    </div>
  </li>;
}
