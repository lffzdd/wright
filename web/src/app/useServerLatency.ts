import { useEffect, useState } from "react";
import { api } from "../api";

/** Round-trip time of a health check, every ten seconds; null while unreachable. */
export function useServerLatency(enabled: boolean): number | null {
  const [latency, setLatency] = useState<number | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let mounted = true;
    let pending = false;
    const measure = async () => {
      if (pending) return;
      pending = true;
      const startedAt = performance.now();
      try {
        const result = await api.health();
        if (mounted && result.ok) setLatency(Math.round(performance.now() - startedAt));
        else if (mounted) setLatency(null);
      } catch {
        if (mounted) setLatency(null);
      } finally {
        pending = false;
      }
    };
    void measure();
    const timer = window.setInterval(() => void measure(), 10_000);
    return () => { mounted = false; window.clearInterval(timer); };
  }, [enabled]);
  return latency;
}
