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
import { toast } from 'sonner';
import { z } from 'zod';

import { useCurrentUser } from '@/lib/auth';

import { orderKeys } from '../api/orders';
import { scheduleCapacityKeys } from '../api/scheduleCapacity';
import { scheduleResultKeys } from '../api/scheduleResult';

const wsEnvelopeSchema = z
  .object({
    type: z.string(),
    message: z.string().optional(),
    reason: z.string().optional(),
    order_number: z.string().optional(),
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
        const description = env.message ?? env.reason ?? '排程器拒絕這次操作，請檢查訂單日期與產能。';
        toast.error('排程失敗', { description });
      }
      if (env.type === 'schedule.materialized') {
        toast.info('排程結果已更新', {
          description: '已同步訂單狀態、生產日期與每日剩餘產能。',
        });
      }
    };

    return () => {
      ws.close();
    };
  }, [user, qc]);
}
