/**
 * Create / Edit order modal.
 *
 * When `order` prop is undefined → create mode.
 * When `order` prop is provided  → edit mode (version_id is forwarded for
 * optimistic-lock protection).
 */
import { zodResolver } from '@hookform/resolvers/zod';
import { useEffect, useMemo } from 'react';
import { useForm } from 'react-hook-form';
import { toast } from 'sonner';
import { z } from 'zod';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useAssignableUsers } from '@/features/auth/api/users';
import { ApiError } from '@/lib/apiFetch';
import { toastApiError } from '@/lib/toastApiError';

import { useCreateOrder, useUpdateOrder } from '../api/orders';
import { useScheduleCapacity } from '../api/scheduleCapacity';
import type { Order } from '../types';

// ---------------------------------------------------------------------------
// Date helpers — mirror backend `_validate_deadline_or_422`, whose window is
// [base_date + 1, base_date + HORIZON_DAYS]. The authoritative base_date and
// horizon come from the scheduler (see useScheduleCapacity); we only fall back
// to a hard-coded horizon when that query hasn't loaded yet.
//
// All arithmetic is done in UTC. The backend derives "today" from
// datetime.now(UTC).date(); computing the client window in local time would
// drift one day ahead during the UTC+8 morning (Taiwan 00:00–07:59), wrongly
// rejecting deadlines the backend would accept.
// ---------------------------------------------------------------------------

const FALLBACK_HORIZON_DAYS = 30;

function todayUTCISO(): string {
  return new Date().toISOString().slice(0, 10);
}

function addDaysToISO(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

// ---------------------------------------------------------------------------
// Form schema — matches backend CreateOrderRequest / UpdateOrderRequest
// ---------------------------------------------------------------------------

const formSchema = z.object({
  customer_name: z.string().min(1, '請填寫客戶名稱').max(255),
  wafer_quantity: z
    .number({ invalid_type_error: '請輸入數字' })
    .int('請輸入整數')
    .min(25, '最少 25 片')
    .max(2500, '最多 2500 片'),
  requested_delivery_date: z.string().min(1, '請選擇要求交貨日'),
  notes: z.string().max(2000).nullable().optional(),
  assigned_to_email: z.string().optional(),
});

type FormValues = z.infer<typeof formSchema>;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface OrderModalProps {
  open: boolean;
  onClose: () => void;
  order?: Order | undefined;
}

export function OrderModal({ open, onClose, order }: OrderModalProps): JSX.Element {
  const isEdit = order !== undefined;
  const createMutation = useCreateOrder();
  const updateMutation = useUpdateOrder();
  const isPending = createMutation.isPending || updateMutation.isPending;
  const users = useAssignableUsers();
  const assignedToDisabled = isEdit;
  const { data: capacity } = useScheduleCapacity();

  const { tomorrowISO, horizonEndISO } = useMemo(() => {
    // Prefer the scheduler's authoritative UTC base_date + horizon
    // (entries.length is exactly SCHEDULER_HORIZON_DAYS) so the client window
    // matches backend validation regardless of timezone or a horizon config
    // change. Fall back to the UTC wall-clock date + 30 only while the
    // capacity query is still loading.
    const baseDate = capacity?.base_date ?? todayUTCISO();
    const horizon = capacity?.entries.length ?? FALLBACK_HORIZON_DAYS;
    return {
      tomorrowISO: addDaysToISO(baseDate, 1),
      horizonEndISO: addDaysToISO(baseDate, horizon),
    };
  }, [capacity]);

  const dynamicSchema = useMemo(() => {
    // Skip the assignee refinement in edit mode: the field is disabled there
    // (see `assignedToDisabled` below) so the user can't fix a mismatch
    // anyway. Without this guard, an order whose original assignee has since
    // been deactivated would fail validation on a field the user can't edit,
    // leaving the modal permanently unsubmittable.
    //
    // We use the capacity payload only for its base_date + horizon length
    // (above). We deliberately do NOT block on per-day
    // ``entries[date].remaining``: ``requested_delivery_date`` is a deadline,
    // not a production date — the scheduler can still admit an order whose
    // deadline-day is full by producing earlier and back-filling. The
    // producer-side `_validate_deadline_or_422` only enforces the horizon
    // window; real admission control lives in the worker's compound finalize.
    return formSchema
      .refine(
        (data) => !data.requested_delivery_date || data.requested_delivery_date >= tomorrowISO,
        {
          message: '交貨日必須是明天之後',
          path: ['requested_delivery_date'],
        },
      )
      .refine(
        (data) => !data.requested_delivery_date || data.requested_delivery_date <= horizonEndISO,
        {
          message: '交貨日超過 30 天排程範圍，請改選較近的日期',
          path: ['requested_delivery_date'],
        },
      )
      .refine(
        (data) => {
          if (isEdit) return true;
          if (!data.assigned_to_email) return true;
          return users.some((u) => u.email === data.assigned_to_email);
        },
        {
          message: '負責人必須是系統中現有的使用者',
          path: ['assigned_to_email'],
        },
      );
  }, [users, isEdit, tomorrowISO, horizonEndISO]);

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(dynamicSchema),
    defaultValues: {
      customer_name: '',
      wafer_quantity: 100,
      requested_delivery_date: '',
      notes: '',
      assigned_to_email: '',
    } as FormValues,
  });

  useEffect(() => {
    if (order) {
      const existingEmail = users.find((u) => u.id === order.assigned_to)?.email ?? '';
      reset({
        customer_name: order.customer_name,
        wafer_quantity: order.wafer_quantity,
        requested_delivery_date: order.requested_delivery_date,
        notes: order.notes ?? '',
        assigned_to_email: existingEmail,
      });
    } else {
      reset({
        customer_name: '',
        wafer_quantity: 100,
        requested_delivery_date: '',
        notes: '',
        assigned_to_email: '',
      });
    }
  }, [order, reset, users]);

  const onSubmit = handleSubmit((values) => {
    const matchedUser = users.find((u) => u.email === values.assigned_to_email);
    const assignedTo = matchedUser?.id ?? null;
    const notes = values.notes !== '' ? (values.notes ?? null) : null;

    if (order) {
      updateMutation.mutate(
        {
          id: order.id,
          payload: {
            wafer_quantity: values.wafer_quantity,
            requested_delivery_date: values.requested_delivery_date,
            notes,
            version_id: order.version_id,
          },
        },
        {
          onSuccess: () => {
            toast.success('訂單已修改', {
              description: '系統已送出排程更新，排程結果會在完成後同步。',
            });
            onClose();
          },
          onError: (err) => {
            toastApiError('修改訂單失敗', err);
          },
        },
      );
    } else {
      createMutation.mutate(
        {
          customer_name: values.customer_name,
          wafer_quantity: values.wafer_quantity,
          requested_delivery_date: values.requested_delivery_date,
          notes,
          assigned_to: assignedTo,
        },
        {
          onSuccess: (createdOrder) => {
            toast.success('訂單已新增', {
              description: `訂單 ${createdOrder.order_number} 已建立，等待排程器處理。`,
            });
            onClose();
          },
          onError: (err) => {
            toastApiError('新增訂單失敗', err);
          },
        },
      );
    }
  });

  const submitLabel = isEdit ? '儲存' : '新增';

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) onClose();
      }}
    >
      <DialogContent data-testid="order-modal">
        <DialogHeader>
          <DialogTitle>{isEdit ? '編輯訂單' : '新增訂單'}</DialogTitle>
        </DialogHeader>

        <form
          id="order-form"
          onSubmit={(e) => {
            void onSubmit(e);
          }}
          className="space-y-4"
          noValidate
        >
          {/* Customer name — read-only in edit mode (backend UpdateOrderRequest excludes it) */}
          <div className="space-y-2">
            <Label htmlFor="customer_name">客戶名稱{!isEdit && ' *'}</Label>
            <Input
              id="customer_name"
              disabled={isEdit}
              aria-invalid={!!errors.customer_name}
              aria-describedby={errors.customer_name ? 'customer_name-error' : undefined}
              data-testid="order-customer-name-input"
              {...register('customer_name')}
            />
            {errors.customer_name && (
              <p id="customer_name-error" role="alert" className="text-xs text-destructive">
                {errors.customer_name.message}
              </p>
            )}
          </div>

          {/* Wafer quantity */}
          <div className="space-y-2">
            <Label htmlFor="wafer_quantity">晶圓數量 * （25 – 2500 片）</Label>
            <Input
              id="wafer_quantity"
              type="number"
              min={25}
              max={2500}
              aria-invalid={!!errors.wafer_quantity}
              aria-describedby={errors.wafer_quantity ? 'wafer_quantity-error' : undefined}
              data-testid="order-wafer-quantity-input"
              {...register('wafer_quantity', { valueAsNumber: true })}
            />
            {errors.wafer_quantity && (
              <p id="wafer_quantity-error" role="alert" className="text-xs text-destructive">
                {errors.wafer_quantity.message}
              </p>
            )}
          </div>

          {/* Requested delivery date */}
          <div className="space-y-2">
            <Label htmlFor="requested_delivery_date">要求交貨日 *</Label>
            <Input
              id="requested_delivery_date"
              type="date"
              min={tomorrowISO}
              max={horizonEndISO}
              aria-invalid={!!errors.requested_delivery_date}
              aria-describedby={
                errors.requested_delivery_date ? 'requested_delivery_date-error' : undefined
              }
              data-testid="order-requested-delivery-date-input"
              {...register('requested_delivery_date')}
            />
            {errors.requested_delivery_date && (
              <p
                id="requested_delivery_date-error"
                role="alert"
                className="text-xs text-destructive"
              >
                {errors.requested_delivery_date.message}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="assigned_to_email">負責人</Label>
            <Input
              id="assigned_to_email"
              list="users-datalist"
              placeholder="輸入 email 搜尋"
              autoComplete="off"
              disabled={assignedToDisabled}
              aria-invalid={!!errors.assigned_to_email}
              aria-describedby={errors.assigned_to_email ? 'assigned_to_email-error' : undefined}
              {...register('assigned_to_email')}
            />
            <datalist id="users-datalist">
              {users
                .filter((u) => u.email)
                .map((u) => (
                  <option key={u.id} value={u.email ?? ''} />
                ))}
            </datalist>
            {errors.assigned_to_email && (
              <p id="assigned_to_email-error" role="alert" className="text-xs text-destructive">
                {errors.assigned_to_email.message}
              </p>
            )}
          </div>

          {/* Notes */}
          <div className="space-y-2">
            <Label htmlFor="notes">備註</Label>
            <Textarea id="notes" rows={3} data-testid="order-notes-input" {...register('notes')} />
          </div>

          {/* Version conflict — 409 */}
          {updateMutation.isError &&
            updateMutation.error instanceof ApiError &&
            updateMutation.error.status === 409 && (
              <div
                role="alert"
                className="rounded-md border border-yellow-300 bg-yellow-50 p-3 text-sm text-yellow-800 dark:border-yellow-700 dark:bg-yellow-950 dark:text-yellow-300"
              >
                <p className="font-medium">資料版本已更新</p>
                <p className="mt-0.5 text-xs">
                  此訂單已被其他人修改，請關閉後重新開啟以取得最新版本。
                </p>
              </div>
            )}

          {/* Generic mutation error */}
          {(createMutation.isError ||
            (updateMutation.isError &&
              !(
                updateMutation.error instanceof ApiError && updateMutation.error.status === 409
              ))) && (
            <p role="alert" className="text-xs text-destructive">
              {(createMutation.error ?? updateMutation.error)?.message ?? '操作失敗，請重試。'}
            </p>
          )}
        </form>

        <DialogFooter>
          <Button variant="outline" type="button" onClick={onClose} disabled={isPending}>
            取消
          </Button>
          <Button
            type="submit"
            form="order-form"
            disabled={isPending}
            data-testid="order-modal-submit-button"
          >
            {isPending ? '儲存中…' : submitLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
