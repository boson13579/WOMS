/**
 * Ring buffer of the last N RED-metrics samples for sparkline rendering.
 *
 * The /system/red endpoint returns point-in-time aggregates, not a time
 * series. Rather than asking the backend for a /timeseries variant (Plan
 * A out of scope), the frontend keeps a tiny in-memory ring buffer that
 * is appended on every successful poll. 20 entries at the 10 s poll
 * cadence gives a ~200 s rolling visual — enough to see "did the spike
 * just start" without server changes.
 *
 * Per RULES.md §2 client-only state lives in Zustand. This buffer
 * deliberately resets on page navigation: the operator is on the page
 * when they care, and persisting across sessions would surface stale
 * data after a multi-hour idle.
 */
import { create } from 'zustand';

/** Maximum samples retained per metric — ~200s at 10s polling cadence. */
export const RED_HISTORY_CAPACITY = 20;

/** Sparkline-renderable metric keys (one ring buffer each). */
export type RedHistoryMetric = 'rate' | 'errorPct' | 'p95' | 'lagP95';

interface RedHistoryState {
  /** Per-metric ring buffer; oldest sample first, newest last. */
  series: Record<RedHistoryMetric, number[]>;
  /**
   * Append one sample per provided metric key. Partial: callers push
   * only the metrics they own (``useRedMetrics`` pushes rate/errorPct/
   * p95; ``useScheduleLag`` pushes lagP95) so adding a new metric to
   * the union doesn't break existing pushers. Implementation slices the
   * front off when capacity is exceeded so each array stays bounded.
   */
  push: (sample: Partial<Record<RedHistoryMetric, number>>) => void;
  /** Clear all buffers — used when the time-range window changes. */
  reset: () => void;
}

function emptySeries(): RedHistoryState['series'] {
  return { rate: [], errorPct: [], p95: [], lagP95: [] };
}

export const useRedHistoryStore = create<RedHistoryState>((set) => ({
  series: emptySeries(),
  push: (sample) => {
    set((state) => {
      const next = { ...state.series };
      (Object.keys(sample) as RedHistoryMetric[]).forEach((key) => {
        const value = sample[key];
        if (value === undefined) return;
        const prev = next[key];
        const appended = prev.length >= RED_HISTORY_CAPACITY ? prev.slice(1) : prev.slice();
        appended.push(value);
        next[key] = appended;
      });
      return { series: next };
    });
  },
  reset: () => {
    set({ series: emptySeries() });
  },
}));
