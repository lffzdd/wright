import { useRef, type PointerEvent } from "react";

/**
 * A vertical drag handle for a side panel. `edge` says which side of the
 * panel the handle sits on: "end" for a left panel (dragging right widens it),
 * "start" for a right panel (dragging left widens it).
 */
export function ResizeHandle({
  width,
  min,
  max,
  edge,
  className,
  resizingClass,
  label,
  hint,
  clamp,
  reset,
  onWidthChange,
  onCommit,
}: {
  width: number;
  min: number;
  max: number;
  edge: "start" | "end";
  className: string;
  /** Body class while dragging, so panels can drop their width transitions. */
  resizingClass: string;
  label: string;
  hint: string;
  clamp: (width: number) => number;
  reset: () => number;
  onWidthChange: (width: number) => void;
  onCommit: (width: number) => void;
}) {
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const sign = edge === "end" ? 1 : -1;

  const finish = (next: number) => {
    dragRef.current = null;
    document.body.classList.remove(resizingClass);
    onWidthChange(next);
    onCommit(next);
  };

  const widthFromEvent = (event: PointerEvent) => {
    if (!dragRef.current) return width;
    return clamp(dragRef.current.startWidth + sign * (event.clientX - dragRef.current.startX));
  };

  const release = (event: PointerEvent<HTMLDivElement>) => {
    if ("releasePointerCapture" in event.currentTarget) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  return (
    <div
      className={className}
      role="separator"
      aria-orientation="vertical"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={width}
      aria-label={label}
      title={hint}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        if ("setPointerCapture" in event.currentTarget) event.currentTarget.setPointerCapture(event.pointerId);
        dragRef.current = { startX: event.clientX, startWidth: width };
        document.body.classList.add(resizingClass);
      }}
      onPointerMove={(event) => {
        if (!dragRef.current) return;
        onWidthChange(widthFromEvent(event));
      }}
      onPointerUp={(event) => {
        if (!dragRef.current) return;
        release(event);
        finish(widthFromEvent(event));
      }}
      onPointerCancel={(event) => {
        if (!dragRef.current) return;
        release(event);
        finish(widthFromEvent(event));
      }}
      onDoubleClick={() => {
        const next = reset();
        onWidthChange(next);
        onCommit(next);
      }}
    />
  );
}
