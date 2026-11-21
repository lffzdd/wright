import { CaretRight } from "@phosphor-icons/react";
import type { ReactNode } from "react";

/** A fold in the Changes list (build output, a directory); it shares the file row's chevron and padding. */
export function ChangeFold({ className, title, aside, open, children }: {
  className: string;
  title: ReactNode;
  /** Fills the row's stats and actions columns, if the fold has any. */
  aside?: ReactNode;
  open?: boolean;
  children: ReactNode;
}) {
  return <details className={`changes-fold ${className}`} open={open}>
    <summary>
      <CaretRight className="chev" size={14} aria-hidden="true" />
      <span className="changes-fold-title">{title}</span>
      {aside}
    </summary>
    <div className="changes-file-list">{children}</div>
  </details>;
}

/** Empty slots as wide as a row's action buttons, so a fold's stats line up with the rows'. */
export function ActionSlots({ count }: { count: number }) {
  return <span className="diff-actions" aria-hidden="true">
    {Array.from({ length: count }, (_, index) => <i key={index} className="changes-action-slot" />)}</span>;
}
