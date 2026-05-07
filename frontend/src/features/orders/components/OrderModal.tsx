/**
 * Create / Edit order modal.
 *
 * When `order` prop is undefined → create mode.
 * When `order` prop is provided  → edit mode (version_id is forwarded for
 * optimistic-lock protection).
 */
import { useEffect } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
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
  assigned_to: z.string().uuid('格式不正確').nullable().optional().or(z.literal('')),
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
      assigned_to: '',
    },
  });

  useEffect(() => {
    if (order) {
      reset({
        customer_name: order.customer_name,
        wafer_quantity: order.wafer_quantity,
        requested_delivery_date: order.requested_delivery_date,
        notes: order.notes ?? '',
        assigned_to: order.assigned_to ?? '',
      });
    } else {
      reset({
        customer_name: '',
        wafer_quantity: 100,
        requested_delivery_date: '',
        notes: '',
        assigned_to: '',
      });
    }
  }, [order, reset]);

  const onSubmit = handleSubmit((values) => {
    const assigned_to = values.assigned_to || null;
    const notes = values.notes || null;

    if (isEdit && order) {
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
          assigned_to,
        },
        { onSuccess: onClose },
      );
    }
  });

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
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
          {/* Customer name */}
          <div className="space-y-1">
            <Label htmlFor="customer_name">客戶名稱 *</Label>
            <Input id="customer_name" {...register('customer_name')} />
            {errors.customer_name && (
              <p className="text-xs text-destructive">{errors.customer_name.message}</p>
            )}
          </div>

          {/* Wafer quantity */}
          <div className="space-y-1">
            <Label htmlFor="wafer_quantity">晶圓數量 * （25 – 2500 片）</Label>
            <Input
              id="wafer_quantity"
              type="number"
              min={25}
              max={2500}
              {...register('wafer_quantity', { valueAsNumber: true })}
            />
            {errors.wafer_quantity && (
              <p className="text-xs text-destructive">{errors.wafer_quantity.message}</p>
            )}
          </div>

          {/* Requested delivery date */}
          <div className="space-y-1">
            <Label htmlFor="requested_delivery_date">要求交貨日 *</Label>
            <Input
              id="requested_delivery_date"
              type="date"
              {...register('requested_delivery_date')}
            />
            {errors.requested_delivery_date && (
              <p className="text-xs text-destructive">
                {errors.requested_delivery_date.message}
              </p>
            )}
          </div>

          {!isEdit && (
            <div className="space-y-1">
              <Label htmlFor="assigned_to">負責人 UUID（選填）</Label>
              <Input
                id="assigned_to"
                placeholder="留空表示未指派"
                {...register('assigned_to')}
              />
              {errors.assigned_to && (
                <p className="text-xs text-destructive">{errors.assigned_to.message}</p>
              )}
            </div>
          )}

          {/* Notes */}
          <div className="space-y-1">
            <Label htmlFor="notes">備註</Label>
            <Textarea id="notes" rows={3} {...register('notes')} />
          </div>

          {/* Mutation error */}
          {(createMutation.isError || updateMutation.isError) && (
            <p className="text-xs text-destructive">
              {(createMutation.error ?? updateMutation.error)?.message ?? '操作失敗，請重試。'}
            </p>
          )}
        </form>

        <DialogFooter>
          <Button variant="outline" type="button" onClick={onClose} disabled={isPending}>
            取消
          </Button>
          <Button type="submit" form="order-form" disabled={isPending}>
            {isPending ? '儲存中…' : isEdit ? '儲存' : '新增'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}