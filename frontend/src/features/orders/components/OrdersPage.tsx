import { Plus } from 'lucide-react';
import { useCallback, useState } from 'react';

import { Header } from '@/components/layout/Header';
import { Button } from '@/components/ui/button';

import { useTriggerSchedule } from '../api/orders';
import { useScheduleWs } from '../hooks/useScheduleWs';
import type { Order } from '../types';

import { OrderFilters } from './OrderFilters';
import { OrderModal } from './OrderModal';
import { OrderTable } from './OrderTable';

export function OrdersPage(): JSX.Element {
  const [modalOpen, setModalOpen] = useState(false);
  const [editingOrder, setEditingOrder] = useState<Order | undefined>(undefined);

  const [scheduleTaskId, setScheduleTaskId] = useState<string | null>(null);
  const triggerSchedule = useTriggerSchedule();

  useScheduleWs(scheduleTaskId);

  const handleNewOrder = useCallback(() => {
    setEditingOrder(undefined);
    setModalOpen(true);
  }, []);

  const handleEdit = useCallback((order: Order) => {
    setEditingOrder(order);
    setModalOpen(true);
  }, []);

  const handleSchedule = useCallback(
    (orderId: string) => {
      triggerSchedule.mutate(orderId, {
        onSuccess: (res) => {
          setScheduleTaskId(res.task_id);
          setTimeout(() => {
            setScheduleTaskId(null);
          }, 30_000);
        },
      });
    },
    [triggerSchedule],
  );

  return (
    <>
      <Header title="訂單列表" />

      <div className="px-6 py-6 space-y-5">
        <div className="flex items-center justify-between gap-4">
          <Button onClick={handleNewOrder} size="sm">
            <Plus className="mr-1.5 h-4 w-4" />
            新增訂單
          </Button>
        </div>

        <OrderFilters />

        <OrderTable onEdit={handleEdit} onSchedule={handleSchedule} />
      </div>

      <OrderModal
        open={modalOpen}
        onClose={() => {
          setModalOpen(false);
        }}
        order={editingOrder}
      />
    </>
  );
}
