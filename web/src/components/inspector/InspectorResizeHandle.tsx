import { useRef, type PointerEvent } from "react";
import { useT } from "../../i18n";
import { clampInspectorWidth, defaultInspectorWidth } from "../../inspectorWidth";

export function InspectorResizeHandle({
  width,
  onWidthChange,
  onCommit,
}: {
  width: number;
  onWidthChange: (width: number) => void;
  onCommit: (width: number) => void;
}) {
  const tr = useT();
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null);

  const finish = (next: number) => {
    dragRef.current = null;
    document.body.classList.remove("inspector-resizing");
    onWidthChange(next);
    onCommit(next);
  };

  const widthFromEvent = (event: PointerEvent) => {
    if (!dragRef.current) return width;
    const delta = dragRef.current.startX - event.clientX;
    return clampInspectorWidth(dragRef.current.startWidth + delta);
  };

  return (
    <div
      className="inspector-resize-handle"
      role="separator"
      aria-orientation="vertical"
      aria-valuemin={280}
      aria-valuemax={560}
      aria-valuenow={width}
      aria-label={tr("web.resize_inspector")}
      title={tr("web.resize_inspector_hint")}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        if ("setPointerCapture" in event.currentTarget) {
          event.currentTarget.setPointerCapture(event.pointerId);
        }
        dragRef.current = { startX: event.clientX, startWidth: width };
        document.body.classList.add("inspector-resizing");
      }}
      onPointerMove={(event) => {
        if (!dragRef.current) return;
        onWidthChange(widthFromEvent(event));
      }}
      onPointerUp={(event) => {
        if (!dragRef.current) return;
        if ("releasePointerCapture" in event.currentTarget) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
        finish(widthFromEvent(event));
      }}
      onPointerCancel={(event) => {
        if (!dragRef.current) return;
        if ("releasePointerCapture" in event.currentTarget) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
        finish(widthFromEvent(event));
      }}
      onDoubleClick={() => {
        const reset = defaultInspectorWidth();
        onWidthChange(reset);
        onCommit(reset);
      }}
    />
  );
}
