import type { ReactNode } from "react";

/** Stroke icons copied from agent-harness-ui (`icon()`, 24 viewBox). */

export function SidebarIcon({ size = 16 }: { size?: number }) {
  return <svg className="ic" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M9 4v16" />
  </svg>;
}

export function PanelIcon({ size = 16 }: { size?: number }) {
  return <svg className="ic" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M15 4v16" />
  </svg>;
}

export function LogoMark({ size = 14, strokeWidth = 2.4 }: { size?: number; strokeWidth?: number }) {
  return <svg className="ic" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M6 18V6l6 6 6-6v12" />
  </svg>;
}

export function CopyIcon({ size = 16 }: { size?: number }) {
  return <svg className="ic" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="9" y="9" width="12" height="12" rx="2" />
    <path d="M5 15V5a2 2 0 0 1 2-2h10" />
  </svg>;
}

export function ShareIcon({ size = 16 }: { size?: number }) {
  return <svg className="ic" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5M12 3v12M7 8l5-5 5 5" />
  </svg>;
}

/** agent-harness-ui `chevrons`: the up/down mark on the project switcher. */
export function ChevronsIcon({ size = 16 }: { size?: number }) {
  return <svg className="ic" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="m7 15 5 5 5-5M7 9l5-5 5 5" />
  </svg>;
}

/** agent-harness-ui `branch`. */
export function BranchIcon({ size = 11 }: { size?: number }) {
  return <svg className="ic" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="6" cy="5" r="2" /><circle cx="6" cy="19" r="2" /><circle cx="18" cy="7" r="2" /><path d="M6 7v10M18 9c0 5-7 4-11 8" />
  </svg>;
}

/** agent-harness-ui `dots`. */
export function DotsIcon({ size = 14 }: { size?: number }) {
  return <svg className="ic" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="5" cy="12" r="1.3" fill="currentColor" /><circle cx="12" cy="12" r="1.3" fill="currentColor" /><circle cx="19" cy="12" r="1.3" fill="currentColor" />
  </svg>;
}

/** The same rounded 1.8 stroke as the icons above, for small action buttons. */
function StrokeIcon({ size, children }: { size: number; children: ReactNode }) {
  return <svg
    className="ic" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
  >{children}</svg>;
}

export function CheckIcon({ size = 16 }: { size?: number }) {
  return <StrokeIcon size={size}><path d="m5 12.5 4.5 4.5L19 7" /></StrokeIcon>;
}

export function EditIcon({ size = 16 }: { size?: number }) {
  return <StrokeIcon size={size}><path d="M4 20h4L19 9a2.83 2.83 0 0 0-4-4L4 16v4ZM13.5 6.5l4 4" /></StrokeIcon>;
}

export function ForkIcon({ size = 16 }: { size?: number }) {
  return <StrokeIcon size={size}>
    <circle cx="6" cy="5" r="2" /><circle cx="18" cy="5" r="2" /><circle cx="12" cy="19" r="2" />
    <path d="M6 7v1a4 4 0 0 0 4 4h4a4 4 0 0 0 4-4V7M12 12v5" />
  </StrokeIcon>;
}

export function RegenerateIcon({ size = 16 }: { size?: number }) {
  return <StrokeIcon size={size}><path d="M4 12a8 8 0 1 0 2.4-5.7L4 8.5M4 4v4.5h4.5" /></StrokeIcon>;
}

type PageIconProps = { size?: number; className?: string };
const pageSvg = (size: number, className: string | undefined, children: ReactNode) => <svg
  className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
  strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;

/** A page with a folded top corner; corners rounder than Phosphor's `File`. */
export function FilePageIcon({ size = 15, className }: PageIconProps) {
  return pageSvg(size, className, <>
    <path d="M13.5 3H8a3.5 3.5 0 0 0-3.5 3.5v11A3.5 3.5 0 0 0 8 21h8a3.5 3.5 0 0 0 3.5-3.5V9z" />
    <path d="M13.5 3v3.5a2.5 2.5 0 0 0 2.5 2.5h3.5" />
  </>);
}

/** A note with a folded bottom corner and two text lines; corners rounder than Phosphor's `Note`. */
export function NotePageIcon({ size = 15, className }: PageIconProps) {
  return pageSvg(size, className, <>
    <path d="M14.5 20.5H7.5A3.5 3.5 0 0 1 4 17V7a3.5 3.5 0 0 1 3.5-3.5h9A3.5 3.5 0 0 1 20 7v8z" />
    <path d="M14.5 20.5V17.5a2.5 2.5 0 0 1 2.5-2.5h3" />
    <path d="M8.5 8.5h7M8.5 12h4" />
  </>);
}
