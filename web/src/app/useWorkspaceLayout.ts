import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { clampInspectorWidth, defaultInspectorWidth } from "../components/inspector/inspectorWidth";
import { clampSidebarWidth, SIDEBAR_WIDTH_DEFAULT } from "../components/session/sidebarWidth";

const COMPACT_QUERY = "(max-width: 1024px)";

/**
 * Which side panels are shown and how wide. On wide screens the session list
 * is docked and can be folded away (sidebarOpen); on compact ones it is a
 * drawer (railOpen). The inspector is docked on wide screens and covers the
 * conversation on compact ones (narrowInspector). Open state and widths are
 * saved as preferences.
 */
export function useWorkspaceLayout() {
  const [railOpen, setRailOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [inspectorWidth, setInspectorWidth] = useState(() => defaultInspectorWidth());
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [sidebarWidth, setSidebarWidth] = useState(SIDEBAR_WIDTH_DEFAULT);
  const sidebarOpenRef = useRef(sidebarOpen);
  sidebarOpenRef.current = sidebarOpen;
  const [narrowInspector, setNarrowInspector] = useState(false);
  const [compactViewport, setCompactViewport] = useState(() => window.matchMedia?.(COMPACT_QUERY).matches ?? false);
  const visibility = useRef({ open: inspectorOpen, narrow: narrowInspector, compact: compactViewport });
  visibility.current = { open: inspectorOpen, narrow: narrowInspector, compact: compactViewport };

  useEffect(() => {
    const media = window.matchMedia?.(COMPACT_QUERY);
    if (!media) return;
    const changed = () => { setCompactViewport(media.matches); setNarrowInspector(false); };
    media.addEventListener("change", changed);
    return () => media.removeEventListener("change", changed);
  }, []);

  const closeInspector = useCallback(() => {
    setNarrowInspector(false);
    if (!visibility.current.compact) {
      setInspectorOpen(false);
      api.setPreference({ inspector_open: false }).catch(() => undefined);
    }
  }, []);
  const openInspector = useCallback(() => {
    setInspectorOpen(true);
    setNarrowInspector(true);
    setRailOpen(false);
    api.setPreference({ inspector_open: true }).catch(() => undefined);
  }, []);
  const toggleInspector = useCallback(() => {
    const current = visibility.current;
    if (current.compact ? current.open && current.narrow : current.open) closeInspector();
    else openInspector();
  }, [closeInspector, openInspector]);
  const toggleRail = useCallback(() => {
    setNarrowInspector(false);
    if (visibility.current.compact) {
      setRailOpen((prev) => !prev);
      return;
    }
    const next = !sidebarOpenRef.current;
    setSidebarOpen(next);
    api.setPreference({ sidebar_open: next }).catch(() => undefined);
  }, []);
  const commitSidebarWidth = useCallback((width: number) => {
    const next = clampSidebarWidth(width);
    setSidebarWidth(next);
    api.setPreference({ sidebar_width: next }).catch(() => undefined);
  }, []);
  const commitInspectorWidth = useCallback((width: number) => {
    const next = clampInspectorWidth(width);
    setInspectorWidth(next);
    api.setPreference({ inspector_width: next }).catch(() => undefined);
  }, []);

  return {
    railOpen, setRailOpen,
    sidebarOpen, setSidebarOpen, sidebarWidth, setSidebarWidth, commitSidebarWidth, toggleRail,
    inspectorOpen, setInspectorOpen, inspectorWidth, setInspectorWidth, commitInspectorWidth,
    narrowInspector, setNarrowInspector, compactViewport, visibility,
    openInspector, closeInspector, toggleInspector,
  };
}
