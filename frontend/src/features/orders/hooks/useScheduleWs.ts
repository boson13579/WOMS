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
 * mutation that started them, not in here. The exception is a failed compound:
 * without a global fallback, rejected scheduler operations can be silent when
 * the calendar dialog is not the active page.
 */
import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { toast } from 'sonner';
import { z } from 'zod';

import { useCurrentUser } from '@/lib/auth';

import { orderKeys } from '../api/orders';
import { scheduleCapacityKeys } from '../api/scheduleCapacity';
import { scheduleResultKeys } from '../api/scheduleResult';

const wsEnvelopeSchema = z
  .object({
    type: z.string(),
    reason: z.string().optional(),
    detail: z.string().optional(),
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
      let env: z.infer<typeof wsEnvelopeSchema>;
      try {
        env = wsEnvelopeSchema.parse(JSON.parse(evt.data));
      } catch {
        return;
      }
      if (env.type.startsWith('schedule.')) {
        void qc.invalidateQueries({ queryKey: orderKeys.all });
        void qc.invalidateQueries({ queryKey: scheduleCapacityKeys.all });
        void qc.invalidateQueries({ queryKey: scheduleResultKeys.all });
      }

      if (env.type === 'schedule.compound_failed') {
        const reason = env.reason ?? '排程器拒絕此操作';
        const description = env.detail ? `${reason}: ${env.detail}` : reason;
        toast.error('排程失敗', { description });
      }
    };

    return () => {
      ws.close();
    };
  }, [user, qc]);
}
