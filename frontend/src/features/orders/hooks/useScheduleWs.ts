/**
 * WebSocket hook for real-time schedule progress.
 *
 * Connects to /api/v1/ws/schedule/{taskId}?token=<jwt> and emits Sonner
 * toasts on progress/completion. The Vite dev proxy (ws:true) forwards the
 * connection to the FastAPI backend transparently.
 */
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

import { useAuthStore } from '@/features/auth/stores/authStore';

import { scheduleProgressSchema } from '../api/orders';
import type { ScheduleProgress } from '../types';

interface UseScheduleWsResult {
  progress: ScheduleProgress | null;
  isConnected: boolean;
}

export function useScheduleWs(taskId: string | null): UseScheduleWsResult {
  const token = useAuthStore((s) => s.token);
  const [progress, setProgress] = useState<ScheduleProgress | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!taskId || !token) return undefined;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${window.location.host}/api/v1/ws/schedule/${taskId}?token=${encodeURIComponent(token)}`;

    const toastId = toast.loading('排程進行中…', { description: '正在啟動演算法' });
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => setIsConnected(true);

    ws.onmessage = (evt: MessageEvent<string>) => {
      const data: ScheduleProgress = scheduleProgressSchema.parse(JSON.parse(evt.data));
      setProgress(data);

      if (data.status === 'completed') {
        toast.success('排程完成！', {
          id: toastId,
          description: data.message,
        });
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

    ws.onclose = () => setIsConnected(false);

    return () => {
      ws.close();
    };
  }, [taskId, token]);

  return { progress, isConnected };
}