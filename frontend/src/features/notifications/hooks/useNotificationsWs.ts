import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { toast } from 'sonner';
import { z } from 'zod';

import { useCurrentUser } from '@/lib/auth';

import { notificationKeys } from '../api/notifications';

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

export function useNotificationsWs(): void {
  const user = useCurrentUser();
  const qc = useQueryClient();

  useEffect(() => {
    if (!user) return undefined;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${window.location.host}/api/v1/ws`;
    const ws = new WebSocket(url);

    ws.onmessage = (evt: MessageEvent<string>) => {
      let env: z.infer<typeof wsEnvelopeSchema>;
      try {
        env = wsEnvelopeSchema.parse(JSON.parse(evt.data));
      } catch {
        return;
      }

      if (env.type === 'notification.created' && env.data) {
        // Invalidate all notifications queries to update badge count and list instantly
        void qc.invalidateQueries({ queryKey: notificationKeys.all });

        // Show a premium real-time toast alert
        toast.info('新通知', {
          description: env.data.message,
          duration: 5000,
        });
      }
    };

    return () => {
      ws.close();
    };
  }, [user, qc]);
}
