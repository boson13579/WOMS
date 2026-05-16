/**
 * WebSocket hook for real-time schedule progress on one compound.
 *
 * Connects to /api/v1/ws (single global channel) and listens for events that
 * match the passed compoundId. Cookie auth is automatic for same-origin WS.
 * The hook is active while compoundId is non-null; it disconnects on terminal
 * outcome (compound_failed / schedule.updated after accept / materialized)
 * or unmount.
 *
 * Event filtering:
 *  - schedule.compound_accepted (compound_id == ours) → "accepted" toast
 *  - schedule.compound_failed   (compound_id == ours) → error + close
 *  - schedule.updated / schedule.materialized → success + close, but only
 *    after our compound has been accepted (these events carry no compound_id
 *    so we use the prior accepted event as our anchor).
 */
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { z } from 'zod';

import { useCurrentUser } from '@/lib/auth';

import { orderKeys } from '../api/orders';

const wsEnvelopeSchema = z
  .object({
    type: z.string(),
    compound_id: z.string().optional(),
    reason: z.string().optional(),
    detail: z.string().nullable().optional(),
  })
  .passthrough();

interface UseScheduleWsResult {
  isConnected: boolean;
}

export function useScheduleWs(compoundId: string | null): UseScheduleWsResult {
  const user = useCurrentUser();
  const qc = useQueryClient();
  const [isConnected, setIsConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!compoundId || !user) return undefined;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Cookie is sent automatically by the browser for same-origin WS connections.
    const url = `${protocol}//${window.location.host}/api/v1/ws`;

    const toastId = toast.loading('排程進行中…', { description: '正在啟動演算法' });
    const ws = new WebSocket(url);
    wsRef.current = ws;

    let accepted = false;

    ws.onopen = () => {
      setIsConnected(true);
    };

    ws.onmessage = (evt: MessageEvent<string>) => {
      let env: z.infer<typeof wsEnvelopeSchema>;
      try {
        env = wsEnvelopeSchema.parse(JSON.parse(evt.data));
      } catch {
        return;
      }

      if (env.type === 'schedule.compound_accepted' && env.compound_id === compoundId) {
        accepted = true;
        toast.loading('排程已接受，正在處理…', { id: toastId });
        return;
      }

      if (env.type === 'schedule.compound_failed' && env.compound_id === compoundId) {
        toast.error('排程失敗', {
          id: toastId,
          description: env.detail ?? env.reason ?? '演算法無法完成排程，請稍後重試',
        });
        void qc.invalidateQueries({ queryKey: orderKeys.all });
        ws.close();
        return;
      }

      // schedule.updated / schedule.materialized carry no compound_id (the
      // former is broadcast; the latter is a per-user notify with no id), so
      // we only treat them as our success once our compound was accepted.
      if (accepted && (env.type === 'schedule.updated' || env.type === 'schedule.materialized')) {
        toast.success('排程完成！', { id: toastId });
        void qc.invalidateQueries({ queryKey: orderKeys.all });
        ws.close();
      }
    };

    ws.onerror = () => {
      toast.error('排程失敗', { id: toastId, description: '請稍後重試' });
      setIsConnected(false);
    };

    ws.onclose = () => {
      setIsConnected(false);
    };

    return () => {
      ws.close();
      toast.dismiss(toastId);
    };
  }, [compoundId, user, qc]);

  return { isConnected };
}
