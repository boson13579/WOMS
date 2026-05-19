/**
 * Create / Edit order modal.
 *
 * When `order` prop is undefined → create mode.
 * When `order` prop is provided  → edit mode (version_id is forwarded for
 * optimistic-lock protection).
 */
import { zodResolver } from '@hookform/resolvers/zod';
import { useEffect } from 'react';
import { useForm } from 'react-hook-form';
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

import { useCreateOrder, useUpdateOrder } from '../api/orders';
import type { Order } from '../types';

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

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(formSchema),
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
        { onSuccess: onClose },
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
        { onSuccess: onClose },
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
      <DialogContent>
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
              aria-invalid={!!errors.requested_delivery_date}
              aria-describedby={
                errors.requested_delivery_date ? 'requested_delivery_date-error' : undefined
              }
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
            <Textarea id="notes" rows={3} {...register('notes')} />
          </div>

          {/* Mutation error */}
          {(createMutation.isError || updateMutation.isError) && (
            <p role="alert" className="text-xs text-destructive">
              {(createMutation.error ?? updateMutation.error)?.message ?? '操作失敗，請重試。'}
            </p>
          )}
        </form>

        <DialogFooter>
          <Button variant="outline" type="button" onClick={onClose} disabled={isPending}>
            取消
          </Button>
          <Button type="submit" form="order-form" disabled={isPending}>
            {isPending ? '儲存中…' : submitLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
