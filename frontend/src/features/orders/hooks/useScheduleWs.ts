/**
 * Passive WebSocket listener for schedule updates.
 *
 * Connects to /api/v1/ws while mounted and invalidates the orders cache on
 * any `schedule.*` event so the table stays fresh after the worker drains
 * its queue (manual trigger, auto-enqueue after CRUD, or background
 * advance_day run).
 *
 * The hook is intentionally NOT per-task: backend broadcasts like
 * `schedule.updated` carry no correlation id, so treating them as a
 * single-task signal would mistakenly conflate other users' compounds with
 * the current session. Toasts for the user's own actions live with the
 * mutation that started them, not in here.
 */
import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { z } from 'zod';

import { useCurrentUser } from '@/lib/auth';

import { orderKeys } from '../api/orders';

const wsEnvelopeSchema = z
  .object({
    type: z.string(),
  })
  .passthrough();

export function useScheduleWs(): void {
  const user = useCurrentUser();
  const qc = useQueryClient();

  useEffect(() => {
    if (!user) return undefined;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Cookie is sent automatically by the browser for same-origin WS connections.
    const url = `${protocol}//${window.location.host}/api/v1/ws`;
    const ws = new WebSocket(url);

    ws.onmessage = (evt: MessageEvent<string>) => {
      let env: { type: string };
      try {
        env = wsEnvelopeSchema.parse(JSON.parse(evt.data));
      } catch {
        return;
      }
      if (env.type.startsWith('schedule.')) {
        void qc.invalidateQueries({ queryKey: orderKeys.all });
      }
    };

    return () => {
      ws.close();
    };
  }, [user, qc]);
}
