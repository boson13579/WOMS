import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { toast } from 'sonner';
import { z } from 'zod';

import { useCurrentUser } from '@/lib/auth';

import { notificationKeys } from '../api/notifications';

const WS_PATH = '/api/v1/ws';
const RECONNECT_INITIAL_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const WS_CLOSE_NORMAL = 1000;
const WS_CLOSE_AUTH_FAILED = 4401;
const TOAST_COALESCE_MS = 500;

const wsEnvelopeSchema = z
  .object({
    type: z.string(),
    data: z
      .object({
        id: z.string().uuid(),
        type: z.string(),
        message: z.string(),
        is_read: z.boolean(),
        created_at: z.string(),
      })
      .optional(),
  })
  .passthrough();

function buildWsUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${WS_PATH}`;
}

export function useNotificationsWs(): void {
  const user = useCurrentUser();
  const userId = user?.id;
  const qc = useQueryClient();

  useEffect(() => {
    if (!userId) return undefined;

    let stopped = false;
    let isFirstOpen = true;
    let backoffMs = RECONNECT_INITIAL_MS;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let notificationToastTimer: ReturnType<typeof setTimeout> | null = null;
    let notificationToastCount = 0;
    let latestNotificationMessage = '';

    function flushNotificationToast(): void {
      if (notificationToastCount === 0) return;

      if (notificationToastCount === 1) {
        toast.info('新通知', {
          description: latestNotificationMessage,
          duration: 5000,
        });
      } else {
        toast.info(`${notificationToastCount} 則新通知`, {
          description: '通知中心已更新',
          duration: 5000,
        });
      }

      notificationToastCount = 0;
      latestNotificationMessage = '';
      notificationToastTimer = null;
    }

    function queueNotificationToast(message: string): void {
      notificationToastCount += 1;
      latestNotificationMessage = message;
      if (notificationToastTimer !== null) return;

      notificationToastTimer = setTimeout(flushNotificationToast, TOAST_COALESCE_MS);
    }

    function handleMessage(evt: MessageEvent<string>): void {
      let env: z.infer<typeof wsEnvelopeSchema>;
      try {
        env = wsEnvelopeSchema.parse(JSON.parse(evt.data));
      } catch {
        return;
      }

      if (env.type === 'notification.created' && env.data) {
        void qc.invalidateQueries({ queryKey: notificationKeys.all });
        queueNotificationToast(env.data.message);
      }
    }

    function connect(): void {
      if (stopped) return;
      ws = new WebSocket(buildWsUrl());

      ws.onopen = () => {
        backoffMs = RECONNECT_INITIAL_MS;
        if (!isFirstOpen) {
          void qc.invalidateQueries({ queryKey: notificationKeys.all });
        }
        isFirstOpen = false;
      };

      ws.onmessage = handleMessage;

      ws.onerror = () => {
        // Let onclose decide whether to reconnect. Some browsers fire
        // both events for the same failed socket.
      };

      ws.onclose = (event: CloseEvent) => {
        if (stopped) return;
        if (event.code === WS_CLOSE_AUTH_FAILED || event.code === WS_CLOSE_NORMAL) return;

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
      if (notificationToastTimer !== null) {
        clearTimeout(notificationToastTimer);
        notificationToastTimer = null;
      }
      ws?.close();
    };
  }, [userId, qc]);
}
