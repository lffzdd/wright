import { useT } from "../../../i18n";
import { Switch, Tag } from "../capability";
import { present, type Entry, type SectionContext } from "./entries";
import { McpCard } from "./McpCard";
import type { HooksState, SkillSwitchesState } from "./resources";
import { SecretRow } from "./SecretField";

export function toolsEntries(ctx: SectionContext): Entry[] {
  const { tr } = ctx;
  const searchKeys = ["TAVILY_API_KEY", "BRAVE_API_KEY"];
  const mcpTerms = [tr("web.mcp.title"), "MCP mcp.json", ...(ctx.mcp.data?.servers ?? []).map((server) => server.name)].join(" ");
  const skillTerms = [tr("web.skills.title"), "skills.disabled", ...(ctx.skills.data?.skills ?? []).map((skill) => `${skill.id} ${skill.name}`)].join(" ");
  return present([
    ctx.row("tools.web_search", {}, "search"),
    ...searchKeys.map((name): Entry => ({
      id: name, group: "search", terms: `${name} ${tr("web.tools.search_key")}`,
      node: <SecretRow name={name} hint={tr(`web.tools.key.${name}`)} secrets={ctx.secrets} />,
    })),
    { id: "mcp", group: "mcp", terms: mcpTerms, node: <McpCard key={ctx.projectId ?? ""} mcp={ctx.mcp} hasProject={Boolean(ctx.projectId)} /> },
    { id: "skills", group: "skills", terms: skillTerms, node: <SkillsCard skills={ctx.skills} /> },
    ctx.row("hooks.config", {}, "hooks"),
    { id: "hooks-list", group: "hooks", terms: `${tr("web.hooks.title")} hooks`, node: <HooksList hooks={ctx.hooks} /> },
  ]);
}

function SkillsCard({ skills }: { skills: SkillSwitchesState }) {
  const tr = useT();
  const view = skills.data;
  if (!view) return skills.error ? <p className="settings-problem settings-gated" role="alert">{skills.error}</p> : null;
  return <div className="skill-switches">
    {view.locked && <p className="hint settings-gated">{tr("web.skills.locked")}</p>}
    {view.skills.length === 0 && <p className="hint settings-gated">{tr("web.skills.none")}</p>}
    {view.skills.map((skill) => <div key={skill.id} className="settings-row">
      <div className="runtime-meta">
        <span className="settings-label">{skill.name || skill.id}</span>
        {skill.description && <p className="hint">{skill.description}</p>}
      </div>
      <Switch label={tr("web.skills.toggle", { name: skill.name || skill.id })} checked={skill.enabled} disabled={view.locked}
        onChange={(next) => { skills.toggle(skill.id, next).catch(() => undefined); }} />
    </div>)}
    {view.errors.map((error) => <p key={error} className="settings-problem settings-gated">{error}</p>)}
  </div>;
}

function HooksList({ hooks }: { hooks: HooksState }) {
  const tr = useT();
  const view = hooks.data;
  if (!view) return null;
  if (view.error) return <p className="settings-problem settings-gated" role="alert">{view.error}</p>;
  if (!view.hooks.length) return <p className="hint settings-gated">{tr(view.path ? "web.hooks.empty" : "web.hooks.none")}</p>;
  return <ul className="hook-list">
    {view.hooks.map((hook, index) => <li key={`${hook.event}-${index}`}>
      <Tag>{hook.event}</Tag>
      <span className="settings-label">{hook.name}</span>
      {hook.matcher !== "*" && <span className="hint">{tr("web.hooks.matcher", { matcher: hook.matcher })}</span>}
      <code>{hook.command.join(" ")}</code>
    </li>)}
  </ul>;
}
