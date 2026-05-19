import { useMutation, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';

import { apiFetch, jsonHeaders } from '@/lib/apiFetch';
import { useCurrentUserId } from '@/lib/auth';

import type { Order } from '../types';

import { orderKeys } from './orders';
import { scheduleResultKeys } from './scheduleResult';

const scheduleCompoundResponseSchema = z.object({
  compound_id: z.string().uuid(),
  message: z.string(),
});

export interface ScheduleCompoundResponse {
  compound_id: string;
  message: string;
}

type SchedulableOrder = Pick<
  Order,
  | 'id'
  | 'order_number'
  | 'wafer_quantity'
  | 'requested_delivery_date'
  | 'status'
  | 'is_pinned'
  | 'pinned_production_date'
>;

type ScheduleOperation =
  | {
      op: 'add' | 'remove' | 'unpin';
      order_id: string;
      order_number: string;
      wafer_quantity: number;
      deadline: string;
    }
  | {
      op: 'pin';
      order_id: string;
      order_number: string;
      wafer_quantity: number;
      deadline: string;
      fake_deadline: string;
    };

export interface PinScheduleInput {
  compoundId: string;
  targets: {
    order: SchedulableOrder;
    targetDate: string;
  }[];
}

function buildPinOpsForOrder(order: SchedulableOrder, targetDate: string): ScheduleOperation[] {
  const baseOp = {
    order_id: order.id,
    order_number: order.order_number,
    wafer_quantity: order.wafer_quantity,
    deadline: order.requested_delivery_date,
  };

  if (order.status === 'pending') {
    return [
      { ...baseOp, op: 'add' as const },
      { ...baseOp, op: 'pin' as const, fake_deadline: targetDate },
    ];
  }

  if (order.is_pinned) {
    return [
      { ...baseOp, op: 'unpin' as const },
      { ...baseOp, op: 'pin' as const, fake_deadline: targetDate },
    ];
  }

  return [{ ...baseOp, op: 'pin' as const, fake_deadline: targetDate }];
}

export function usePinScheduleOperation(): ReturnType<
  typeof useMutation<ScheduleCompoundResponse, Error, PinScheduleInput>
> {
  const qc = useQueryClient();
  const userId = useCurrentUserId();

  return useMutation<ScheduleCompoundResponse, Error, PinScheduleInput>({
    mutationFn: (input) => {
      if (!userId) {
        throw new Error('You must be logged in to schedule orders.');
      }

      const ops = input.targets.flatMap(({ order, targetDate }) =>
        buildPinOpsForOrder(order, targetDate),
      );
      return apiFetch(
        '/api/v1/schedule/operations',
        {
          method: 'POST',
          credentials: 'include',
          headers: jsonHeaders(),
          body: JSON.stringify({
            compound_id: input.compoundId,
            group: 'grow',
            op_count: ops.length,
            ops,
            requested_by: userId,
          }),
        },
        (d) => scheduleCompoundResponseSchema.parse(d),
      );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: orderKeys.all });
      void qc.invalidateQueries({ queryKey: scheduleResultKeys.all });
    },
  });
}
