/**
 * Manual scheduler controls — Trigger + Rebuild buttons.
 *
 * Role-gated: rendered only for ``scheduler`` / ``root``. Hidden for
 * ``viewer`` / ``order_manager`` (the backend would 403 anyway; the
 * frontend hide is UX, not authorization).
 *
 * Both buttons are mutations with optimistic toast feedback. They return
 * 202 Accepted with a task id — the actual outcome arrives later via
 * WebSocket (currently polling). The toast is "queued" rather than
 * "complete" to match that async semantic.
 */
import { useMutation } from '@tanstack/react-query';
import { Hammer, Loader2, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { z } from 'zod';

import { Button } from '@/components/ui/button';
import { useCurrentRole } from '@/lib/auth';

import { apiFetch } from '../api/apiFetch';

const triggerResponseSchema = z.object({
  task_id: z.string(),
  message: z.string(),
});

const rebuildResponseSchema = z.object({
  task_id: z.string(),
  message: z.string(),
});

type MutationResponse = z.infer<typeof triggerResponseSchema>;

function postTrigger(): Promise<MutationResponse> {
  return apiFetch('/api/v1/schedule/trigger', { method: 'POST', credentials: 'include' }, (d) =>
    triggerResponseSchema.parse(d),
  );
}

function postRebuild(): Promise<MutationResponse> {
  return apiFetch('/api/v1/schedule/rebuild', { method: 'POST', credentials: 'include' }, (d) =>
    rebuildResponseSchema.parse(d),
  );
}

export function ScheduleControlBar(): JSX.Element | null {
  const role = useCurrentRole();
  const canControl = role === 'scheduler' || role === 'root';

  const triggerMutation = useMutation<MutationResponse>({
    mutationFn: postTrigger,
    onSuccess: (res) => {
      toast.success('Scheduling queued', { description: res.message });
    },
    onError: (err) => {
      toast.error('Failed to trigger scheduling', { description: err.message });
    },
  });

  const rebuildMutation = useMutation<MutationResponse>({
    mutationFn: postRebuild,
    onSuccess: (res) => {
      toast.success('Rebuild queued', { description: res.message });
    },
    onError: (err) => {
      toast.error('Failed to queue rebuild', { description: err.message });
    },
  });

  // Hide entirely for non-control roles — keeps the dashboard layout
  // from getting an empty action area for the majority of users.
  if (!canControl) return null;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        type="button"
        variant="default"
        size="sm"
        onClick={() => {
          triggerMutation.mutate();
        }}
        disabled={triggerMutation.isPending}
      >
        {triggerMutation.isPending ? (
          <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
        ) : (
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
        )}
        Trigger scheduling
      </Button>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => {
          rebuildMutation.mutate();
        }}
        disabled={rebuildMutation.isPending}
      >
        {rebuildMutation.isPending ? (
          <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
        ) : (
          <Hammer className="mr-1.5 h-3.5 w-3.5" aria-hidden />
        )}
        Rebuild
      </Button>
    </div>
  );
}
