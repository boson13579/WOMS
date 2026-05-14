/**
 * WebSocket hook for real-time schedule progress.
 *
 * Connects to /api/v1/ws/schedule/{taskId}?token=<jwt> and emits Sonner
 * toasts on progress/completion. The Vite dev proxy (ws:true) forwards the
 * connection to the FastAPI backend transparently.
 */
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

import { useCurrentUser } from '@/lib/auth';

import { orderKeys, scheduleProgressSchema } from '../api/orders';
import type { ScheduleProgress } from '../types';

interface UseScheduleWsResult {
  progress: ScheduleProgress | null;
  isConnected: boolean;
}

export function useScheduleWs(taskId: string | null): UseScheduleWsResult {
  const user = useCurrentUser();
  const qc = useQueryClient();
  const [progress, setProgress] = useState<ScheduleProgress | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!taskId || !user) return undefined;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Cookie is sent automatically by the browser for same-origin WS connections.
    const url = `${protocol}//${window.location.host}/api/v1/ws/schedule/${taskId}`;

    const toastId = toast.loading('排程進行中…', { description: '正在啟動演算法' });
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setIsConnected(true);
    };

    ws.onmessage = (evt: MessageEvent<string>) => {
      const data: ScheduleProgress = scheduleProgressSchema.parse(JSON.parse(evt.data));
      setProgress(data);

      if (data.status === 'completed') {
        toast.success('排程完成！', {
          id: toastId,
          description: data.message,
        });
        void qc.invalidateQueries({ queryKey: orderKeys.all });
        ws.close();
      } else {
        toast.loading(`排程中 ${data.progress}%`, {
          id: toastId,
          description: data.message,
        });
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

  return { progress, isConnected };
}
