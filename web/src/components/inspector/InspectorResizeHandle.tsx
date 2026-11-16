import { useT } from "../../i18n";
import { clampInspectorWidth, defaultInspectorWidth, INSPECTOR_WIDTH_MAX, INSPECTOR_WIDTH_MIN } from "../../inspectorWidth";
import { ResizeHandle } from "../ui/ResizeHandle";

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
  return <ResizeHandle
    width={width}
    min={INSPECTOR_WIDTH_MIN}
    max={INSPECTOR_WIDTH_MAX}
    edge="start"
    className="inspector-resize-handle"
    resizingClass="inspector-resizing"
    label={tr("web.resize_inspector")}
    hint={tr("web.resize_inspector_hint")}
    clamp={(next) => clampInspectorWidth(next)}
    reset={() => defaultInspectorWidth()}
    onWidthChange={onWidthChange}
    onCommit={onCommit}
  />;
}
