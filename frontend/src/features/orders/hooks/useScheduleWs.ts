/**
 * WebSocket hook for real-time schedule progress.
 *
 * Connects to /api/v1/ws (the single global channel) and listens for
 * schedule-related events. Cookie auth is used automatically for same-origin
 * connections. The hook is active while taskId is non-null; it disconnects on
 * completion, error, or unmount.
 */
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { z } from 'zod';

import { useCurrentUser } from '@/lib/auth';

import { orderKeys } from '../api/orders';

const wsEnvelopeSchema = z.object({
  type: z.string(),
});

interface UseScheduleWsResult {
  isConnected: boolean;
}

export function useScheduleWs(taskId: string | null): UseScheduleWsResult {
  const user = useCurrentUser();
  const qc = useQueryClient();
  const [isConnected, setIsConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!taskId || !user) return undefined;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Cookie is sent automatically by the browser for same-origin WS connections.
    const url = `${protocol}//${window.location.host}/api/v1/ws`;

    const toastId = toast.loading('排程進行中…', { description: '正在啟動演算法' });
    const ws = new WebSocket(url);
    wsRef.current = ws;

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

      if (env.type === 'schedule.compound_accepted') {
        toast.loading('排程已接受，正在處理…', { id: toastId });
      } else if (env.type === 'schedule.compound_failed') {
        toast.error('排程失敗', { id: toastId, description: '演算法無法完成排程，請稍後重試' });
        void qc.invalidateQueries({ queryKey: orderKeys.all });
        ws.close();
      } else if (env.type === 'schedule.updated' || env.type === 'schedule.materialized') {
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
  }, [taskId, user, qc]);

  return { isConnected };
}
