import { CaretRight, Check, X } from "@phosphor-icons/react";
import { useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useT } from "../../i18n";
import type { TimelineItem, ToolState } from "../../types";
import { RevealCallContext } from "../timeline/revealCall";
import { PermissionAutoLine, PermissionDoneLine } from "./PermissionRecord";
import { PlanMark, PlanTimelineCard, planCardPayload, planOf, planRejection } from "./PlanCard";
import { QuestionRecord } from "./QuestionRecord";
import {
  DIFF_PREVIEW_ROWS, DiffBody, DirectoryTree, NumberedContent, PlainOutput, ReadToolbar, SandboxDenialHint, SearchResults, TestFailureOutput, WebResults,
  compactToolBody, directoryEntryCount, isNumbered, looksLikeTestFailure, searchMatches, webHits, type SandboxDenial,
} from "./ToolBodies";
import { isDirectoryTool, referenceToolCompact, referenceToolKind, toolCategory, toolFamily, toolIcon, toolLabel } from "./toolKinds";
import { arg, asRecord, formatDuration, textOf } from "./values";

/** Prefer ToolResult.display.diff, then data.diff for older sessions. */
function editDiffSource(
  family: string,
  data: Record<string, unknown> | null | undefined,
  display: unknown,
): Record<string, unknown> | null | undefined {
  if (family !== "edit") return data;
  const shown = asRecord(display);
  if (typeof shown?.diff === "string") return shown;
  if (typeof data?.diff === "string") return data;
  return shown ?? data;
}

function exitCode(result: Record<string, unknown> | null | undefined, data: unknown): number | null {
  const source = result ?? (data && typeof data === "object" ? data as Record<string, unknown> : null);
  if (!source) return null;
  const value = source.returncode ?? source.exit_code ?? source.exitCode;
  return typeof value === "number" ? value : null;
}

/** Expand only while the call is in flight; fold once it finishes. */
function defaultToolExpanded(phase: string | undefined): boolean {
  return phase === "running";
}

export function ToolCard({
  name, phase, args, result, data, display, duration, output, kind, sessionId, enter, callId,
}: {
  enter?: boolean;
  /** Anchors the card so other views can scroll to this call. */
  callId?: string;
  /** The card's turn is still running. */
  live?: boolean;
  sessionId?: string;
  name: string;
  phase?: string;
  args?: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  data?: unknown;
  /** ToolResult.display: what the interface shows when it differs from the model's data. */
  display?: unknown;
  duration?: number | null;
  output?: string;
  kind?: string;
}) {
  const tr = useT();
  const family = toolFamily(name, kind);
  const reveal = useContext(RevealCallContext);
  const [toggled, setToggled] = useState<boolean | null>(null);
  const phaseRef = useRef(phase);
  useEffect(() => {
    if (phaseRef.current === "running" && phase !== "running") setToggled(null);
    phaseRef.current = phase;
  }, [phase]);
  useEffect(() => {
    if (reveal && callId && reveal.callId === callId) setToggled(true);
  }, [reveal?.callId, reveal?.request, callId]);
  const command = arg(args, ["command", "cmd"]);
  const path = arg(args, ["path", "file", "file_path", "target", "directory"]);
  const pattern = family === "shell" || family === "edit" ? "" : arg(args, ["pattern", "query", "regex"]);
  const code = family === "shell" ? exitCode(result, data) : null;
  const raw = output || (result ? (typeof result.output === "string" ? result.output : JSON.stringify(result, null, 2)) : "");
  const metricSource = data && typeof data === "object" ? data as Record<string, unknown> : result;
  const permission = metricSource?.permission as { decision?: unknown; rule?: string; auto?: boolean } | undefined;
  const denied = phase === "failed" && permission?.decision === "deny";
  if (permission?.auto && permission.rule && phase === "succeeded") {
    return <PermissionAutoLine callId={callId} rule={String(permission.rule)} enter={enter} />;
  }
  if (denied) {
    return <PermissionDoneLine callId={callId} command={command || path || name} decision="deny" enter={enter} />;
  }
  const diffSource = editDiffSource(family, metricSource, display);
  const hasDiff = family === "edit" && typeof diffSource?.diff === "string";
  const body = hasDiff ? diffSource.diff as string : compactToolBody(name, raw, result ?? (data && typeof data === "object" ? data as Record<string, unknown> : null));
  const category = toolCategory(name, kind);
  const isDirectory = category === "directories";
  const isSearch = category === "searches";
  const matches = isSearch ? searchMatches(metricSource) : null;
  const hits = category === "web" ? webHits(metricSource) : null;
  const directory = isDirectory && Array.isArray(metricSource?.entries) ? metricSource : null;
  const content = !isDirectory && !isSearch && family !== "shell" && family !== "edit" && typeof metricSource?.content === "string" ? metricSource.content : null;
  // A plan rule turning an update down is not a tool failure: the plan is unchanged and the model retries.
  const rejection = phase === "failed" && category === "plans" ? planRejection(display) : null;
  const failed = !rejection && (phase === "failed" || (code !== null && code !== 0));
  const refKind = referenceToolKind(name, kind);
  const open = toggled ?? (!denied && !rejection && defaultToolExpanded(phase));
  const Icon = toolIcon(name, category, refKind);
  const bodyLines = body.split("\n").filter(Boolean);
  const added = family === "edit" && typeof diffSource?.additions === "number" ? diffSource.additions : family === "edit" ? bodyLines.filter((line) => line.startsWith("+")).length : 0;
  const deleted = family === "edit" && typeof diffSource?.deletions === "number" ? diffSource.deletions : family === "edit" ? bodyLines.filter((line) => line.startsWith("-")).length : 0;
  const directoryCount = family === "read" && isDirectoryTool(name)
    ? directoryEntryCount(result, data) || (typeof result?.output === "string" && !result.output.trim().startsWith("{") ? bodyLines.length : 0)
    : 0;
  const testFailed = family === "shell" && code !== null && code !== 0 && looksLikeTestFailure(body);
  const durationLabel = typeof duration === "number" ? formatDuration(duration) : "";
  const readRange = category === "reads" && phase !== "failed" && metricSource?.unchanged !== true
    && typeof metricSource?.start_line === "number" && typeof metricSource?.end_line === "number" && metricSource.end_line >= metricSource.start_line
    ? { start: metricSource.start_line, end: metricSource.end_line } : null;
  const planSource = display && typeof display === "object" ? display as Record<string, unknown> : metricSource;
  const plan = category === "plans" && (phase !== "failed" || rejection) ? planOf(planSource) : null;
  const focusId = rejection ? rejection.params.step_id : typeof args?.step_id === "string" ? args.step_id : undefined;
  const planFocus = plan && focusId ? plan.steps.find((step) => step.id === focusId) : undefined;
  const planHeadline = plan ? (planFocus?.title ?? plan.objective) : "";
  const compact = referenceToolCompact(refKind) || (refKind === "tool" && (category === "web" || category === "directories"));
  const status = phase === "running" ? "running" : failed && !denied ? "error" : phase === "succeeded" ? "done" : phase || "";
  const metaParts: ReactNode[] = [];
  if (family === "edit" && phase !== "failed" && (phase === "succeeded" || added + deleted > 0)) metaParts.push(<><span className="edit-add">+{added}</span> <span className="edit-del">−{deleted}</span></>);
  if (directoryCount > 0 && phase === "succeeded") metaParts.push(tr("web.n_files", { count: directoryCount }));
  if (matches && phase === "succeeded") metaParts.push(tr("web.steps.match_count", { count: matches.length, files: new Set(matches.map((match) => String(match.path ?? ""))).size }));
  if (hits && phase === "succeeded") metaParts.push(tr("web.steps.result_count", { count: hits.length }));
  if (readRange) metaParts.push(tr("web.n_lines", { count: readRange.end - readRange.start + 1 }));
  if (plan && plan.steps.length > 0) metaParts.push(tr("web.plan.progress", { done: plan.steps.filter((step) => step.status === "completed").length, total: plan.steps.length }));
  if (code !== null && code !== 0) metaParts.push(<span className="edit-del">exit {code}{testFailed ? ` (${tr("web.bug_confirmed")})` : ""}</span>);
  if (durationLabel) metaParts.push(durationLabel);
  if (plan && plan.steps.length > 0) {
    return <PlanTimelineCard plan={plan} rejection={rejection} body={rejection ? body : ""} enter={enter} callId={callId} />;
  }
  return <div className={`tl-indent${enter ? " enter" : ""}`} data-call-id={callId || undefined}>
    <section className={`tool tool-card${compact ? " compact" : ""}${open ? " open" : ""}`} data-status={status} data-kind={refKind}>
      <button type="button" className="tool-card-head tool-head" aria-expanded={open} onClick={() => setToggled(!(toggled ?? open))}>
        <span className="tool-ic" aria-hidden="true"><Icon size={14} /></span>
        <span className="tool-name">{toolLabel(name)}</span>
        {family === "shell"
          ? <span className="tool-arg mono" title={command || name}>{command || name}</span>
          : (pattern || path) && <span className="tool-arg mono" title={[pattern, path].filter(Boolean).join(" ")}>
            {pattern}{pattern && path ? <span className="tool-arg-dim"> {path}</span> : path}
          </span>}
        {planHeadline && <span className="plan-headline">{planFocus && <PlanMark status={planFocus.status} />}<span>{planHeadline}</span></span>}
        <span className="tool-meta">
          {rejection && <span className="denied-pill">{tr("web.plan.not_applied")}</span>}
          {phase === "failed" && code === null && !rejection && <span className={denied ? "denied-pill" : "fail-pill"}>{tr(denied ? "web.tool_denied" : "web.status.failed")}</span>}
          {metaParts.map((part, index) => <span key={index} className="tool-meta-part">{part}</span>)}
        </span>
        <span className="tool-st" aria-hidden="true">
          {phase === "running" ? <span className="spin" />
            : failed && !denied ? <X size={14} weight="bold" />
              : phase === "succeeded" ? <Check size={14} weight="bold" /> : null}
        </span>
        <CaretRight size={14} className="tool-chev" aria-hidden="true" />
      </button>
      {open && <div className={`tool-card-body tool-body${failed && !denied ? " failed" : ""}`}>
        {family === "shell" ? <div className="term">
          {command && <div className="term-cmd"><span className="term-prompt">$ </span>{command}</div>}
          {rejection && body && <p className="plan-rejection">{body}</p>}
          {body && (testFailed ? <TestFailureOutput text={body} /> : <PlainOutput body={body} failed={code !== null && code !== 0} structured={false} live={phase === "running"} />)}
          {code !== null && phase !== "running" && <div className={`term-exit${code !== 0 ? " failed" : ""}`}>{tr("web.exit_code", { code })}</div>}
          {Boolean(metricSource?.sandbox_denial) && <SandboxDenialHint denial={metricSource!.sandbox_denial as SandboxDenial} sessionId={sessionId} />}
        </div> : <>
          {readRange && path && <ReadToolbar path={path} line={readRange.start} />}
          {rejection && body && <p className="plan-rejection">{body}</p>}
          {directory ? <DirectoryTree source={directory} globStyle={refKind === "glob"} />
            : matches && phase === "succeeded" ? <SearchResults matches={matches} pattern={pattern} literal={metricSource?.fixed_string === true || args?.fixed_string === true} />
            : hits && phase === "succeeded" ? <WebResults hits={hits} />
            : content !== null && isNumbered(content) ? <NumberedContent text={content} path={path} />
            : body && (family === "edit" ? <DiffBody text={body} limit={DIFF_PREVIEW_ROWS} path={path} /> : testFailed ? <TestFailureOutput text={body} /> : isNumbered(body) ? <NumberedContent text={body} path={path} /> : <PlainOutput body={body} failed={code !== null && code !== 0} structured live={phase === "running"} />)}
          {Boolean(metricSource?.sandbox_denial) && <SandboxDenialHint denial={metricSource!.sandbox_denial as SandboxDenial} sessionId={sessionId} />}
          {hasDiff && !body && <p className="empty-small">{tr("web.no_diff")}</p>}
          {hasDiff && diffSource?.diff_truncated === true && <p className="hint" role="status">{tr("web.diff_truncated")}</p>}
        </>}
      </div>}
    </section>
  </div>;
}

function toolOutput(tool: ToolState): string {
  const data = tool.data;
  const record = data && typeof data === "object" ? data as Record<string, unknown> : null;
  const structuredOutput = record ? [record.stdout, record.stderr].filter((value) => typeof value === "string" && value).join("\n") || textOf(record.output) : "";
  // Without command output the error already says what happened; its envelope is diagnostics.
  if (tool.err && !structuredOutput) return tool.err;
  const body = structuredOutput || tool.output || (typeof data === "string" ? data : data == null ? "" : JSON.stringify(data, null, 2));
  return [tool.err, body].filter(Boolean).join("\n");
}

export function toolCardFromTimeline(item: TimelineItem, sessionId?: string, enter?: boolean, live?: boolean) {
  const result = item.result;
  const envelope = result && ("ok" in result || "data" in result || "err" in result);
  const tool: ToolState = {
    call_id: item.call_id || item.id, name: item.name || item.kind,
    arguments: item.arguments, output: item.output,
    data: envelope ? result.data : result,
    display: envelope ? result.display : undefined,
    err: envelope ? textOf(result.err) : undefined,
  };
  if (tool.name === "ask_user") return <QuestionRecord key={item.id} args={tool.arguments} data={tool.data} phase={item.phase} enter={enter} />;
  const data = tool.data && typeof tool.data === "object" ? tool.data as Record<string, unknown> : null;
  const planPayload = planCardPayload(tool.name, item.phase, tool.display, tool.data, data);
  if (planPayload) return <PlanTimelineCard key={item.id} callId={tool.call_id} plan={planPayload.plan} rejection={planPayload.rejection} body={planPayload.body} enter={enter} />;
  return <ToolCard key={item.id} callId={tool.call_id} name={tool.name} phase={item.phase} args={tool.arguments} result={data} display={tool.display} duration={item.duration_ms} output={toolOutput(tool)} kind={item.kind} sessionId={sessionId} enter={enter} live={live} />;
}

export function toolCardFromState(tool: ToolState, sessionId?: string, enter?: boolean, live?: boolean) {
  if (tool.name === "ask_user") return <QuestionRecord key={tool.call_id} args={tool.arguments} data={tool.data} phase={tool.phase} enter={enter} />;
  const data = tool.data && typeof tool.data === "object" ? tool.data as Record<string, unknown> : null;
  const planPayload = planCardPayload(tool.name, tool.phase, tool.display, tool.data, data);
  if (planPayload) return <PlanTimelineCard key={tool.call_id} callId={tool.call_id} plan={planPayload.plan} rejection={planPayload.rejection} body={planPayload.body} enter={enter} />;
  return <ToolCard key={tool.call_id} callId={tool.call_id} name={tool.name} phase={tool.phase} args={tool.arguments} data={tool.data} display={tool.display} output={toolOutput(tool)} sessionId={sessionId} enter={enter} live={live} />;
}
