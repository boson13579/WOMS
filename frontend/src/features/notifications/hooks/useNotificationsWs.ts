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
// Window used to coalesce toasts when a scheduler run bursts several
// notification.created events together — one "N 則新通知" toast reads
// better than N stacked ones.
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
    let pendingMessages: string[] = [];
    let toastTimer: ReturnType<typeof setTimeout> | null = null;

    function flushPendingToasts(): void {
      const msgs = pendingMessages;
      pendingMessages = [];
      toastTimer = null;
      if (msgs.length === 0) return;
      if (msgs.length === 1) {
        toast.info('新通知', { description: msgs[0], duration: 5000 });
        return;
      }
      toast.info(`${msgs.length} 則新通知`, {
        description: msgs[0],
        duration: 5000,
      });
    }

    function handleMessage(evt: MessageEvent<string>): void {
      let env: z.infer<typeof wsEnvelopeSchema>;
      try {
        env = wsEnvelopeSchema.parse(JSON.parse(evt.data));
      } catch {
        return;
      }

      if (env.type === 'notification.created' && env.data) {
        // Invalidate all notifications queries to update badge count and list instantly
        void qc.invalidateQueries({ queryKey: notificationKeys.all });

        // Coalesce bursts (e.g. a scheduler run touching several orders) into
        // a single toast — see TOAST_COALESCE_MS.
        pendingMessages.push(env.data.message);
        if (toastTimer !== null) clearTimeout(toastTimer);
        toastTimer = setTimeout(flushPendingToasts, TOAST_COALESCE_MS);
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
      if (toastTimer !== null) {
        clearTimeout(toastTimer);
        toastTimer = null;
      }
      ws?.close();
    };
  }, [userId, qc]);
}
