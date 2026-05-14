/**
 * `useDashboardWs` — subscribe to backend scheduling events over WebSocket
 * and translate each event into a `queryClient.invalidateQueries` call.
 *
 * Why a hook (not a global subscriber): the connection should live exactly
 * as long as the dashboard page is mounted. Mount opens the socket;
 * unmount tears it down (including any pending reconnect timer) so a
 * navigation away cleans up cleanly.
 *
 * Why no client-state store: per `docs/RULES.md` §2 server-state is owned
 * by React Query. Events carry no payload we render directly — they only
 * tell the dashboard "this slice is stale, refetch it". Storing event
 * envelopes in Zustand would split the source of truth.
 *
 * Why no third-party WS library: the native `WebSocket` plus a hand-rolled
 * exponential backoff is ~70 LoC and avoids pulling in another dependency
 * for one connection.
 *
 * The cookie is auto-attached on a same-origin WebSocket upgrade, so we
 * don't pass a `?token=` query param. Backend `/api/v1/ws` accepts cookie
 * auth (PR #18) and closes with code 4401 when the cookie is missing or
 * the token is invalid.
 */
import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';

import { useCurrentUser } from '@/lib/auth';

const WS_PATH = '/api/v1/ws';

const RECONNECT_INITIAL_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

const WS_CLOSE_NORMAL = 1000;
const WS_CLOSE_AUTH_FAILED = 4401;

/**
 * Map of backend event ``type`` strings → the query keys that should be
 * invalidated when each event arrives. Keys are prefix-matched by React
 * Query, so ``['orders', 'snapshot']`` invalidates all 4 per-status
 * snapshot queries in one call.
 *
 * Keep in lockstep with the event types emitted by
 * ``backend/app/workers/scheduling.py``. Adding a new event type without
 * a matching entry here is intentionally a no-op (the hook silently
 * ignores unknown types so the dashboard never crashes on an envelope
 * shape it doesn't yet understand).
 */
const EVENT_INVALIDATIONS: Readonly<Partial<Record<string, readonly (readonly unknown[])[]>>> = {
  // Broadcast after ``apply_schedule`` writes everything: capacity tree,
  // scheduled dates, status. The dashboard's four polled slices are all
  // potentially stale.
  'schedule.updated': [
    ['schedule', 'capacity'],
    ['schedule', 'pending-ops'],
    ['orders', 'snapshot'],
    ['schedule', 'status'],
  ],
  // Materializer writes ``daily_breakdown`` + scheduled production dates
  // only — pending-ops and schedule status are untouched.
  'schedule.materialized': [
    ['schedule', 'capacity'],
    ['orders', 'snapshot'],
  ],
  // Compound transitions affect the pending queue + the run state flag.
  'schedule.compound_accepted': [
    ['schedule', 'pending-ops'],
    ['schedule', 'status'],
  ],
  'schedule.compound_failed': [
    ['schedule', 'pending-ops'],
    ['schedule', 'status'],
  ],
  // Cancel only removes from the queue; status / capacity unaffected.
  'schedule.compound_cancelled': [['schedule', 'pending-ops']],
  // Rebuild can orphan PATCHes; pending + orders snapshot need refresh.
  'schedule.rebuild_skipped': [
    ['schedule', 'pending-ops'],
    ['orders', 'snapshot'],
  ],
};

function buildWsUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${WS_PATH}`;
}

export function useDashboardWs(): void {
  const queryClient = useQueryClient();
  const user = useCurrentUser();

  useEffect(() => {
    if (!user) return undefined;

    // ``stopped`` is the unmount latch: prevents an already-scheduled
    // reconnect timer from racing past cleanup.
    let stopped = false;
    let backoffMs = RECONNECT_INITIAL_MS;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    function dispatchEvent(rawData: unknown): void {
      if (typeof rawData !== 'string') return;
      let envelope: unknown;
      try {
        envelope = JSON.parse(rawData);
      } catch {
        // Malformed JSON — drop the message silently rather than throw,
        // so one bad frame doesn't break subsequent events.
        return;
      }
      if (typeof envelope !== 'object' || envelope === null) return;
      const { type } = envelope as { type?: unknown };
      if (typeof type !== 'string') return;
      const keys = EVENT_INVALIDATIONS[type];
      if (!keys) return;
      keys.forEach((queryKey) => {
        void queryClient.invalidateQueries({ queryKey });
      });
    }

    function connect(): void {
      if (stopped) return;
      ws = new WebSocket(buildWsUrl());

      ws.onopen = () => {
        // Reset the backoff so a flaky network that took several retries
        // to come back doesn't keep paying 30s waits on the next blip.
        backoffMs = RECONNECT_INITIAL_MS;
      };

      ws.onmessage = (e: MessageEvent) => {
        dispatchEvent(e.data);
      };

      ws.onclose = (e: CloseEvent) => {
        if (stopped) return;
        // 4401 means the cookie auth was rejected; reconnecting would
        // just thrash the server. The user has to re-login (the
        // dashboard widgets will surface their own auth errors via
        // ``apiFetch`` on the next polling tick).
        if (e.code === WS_CLOSE_AUTH_FAILED) return;
        // 1000 is a clean server-initiated shutdown (lifespan exit).
        // We don't want to reconnect to a process that's going down.
        if (e.code === WS_CLOSE_NORMAL) return;

        reconnectTimer = setTimeout(connect, backoffMs);
        backoffMs = Math.min(backoffMs * 2, RECONNECT_MAX_MS);
      };
    }

    connect();

    return () => {
      stopped = true;
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      ws?.close();
    };
  }, [user, queryClient]);
}
