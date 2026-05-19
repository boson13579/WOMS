import { Calendar, Plus } from 'lucide-react';
import { useCallback, useState } from 'react';
import { toast } from 'sonner';

import { Header } from '@/components/layout/Header';
import { Button } from '@/components/ui/button';
import { useCanSchedule, useCanWrite } from '@/lib/auth';

import { useTriggerSchedule } from '../api/orders';
import { useScheduleWs } from '../hooks/useScheduleWs';
import type { Order } from '../types';

import { OrderFilters } from './OrderFilters';
import { OrderModal } from './OrderModal';
import { OrderTable } from './OrderTable';

export function OrdersPage(): JSX.Element {
  const [modalOpen, setModalOpen] = useState(false);
  const [editingOrder, setEditingOrder] = useState<Order | undefined>(undefined);

  const triggerSchedule = useTriggerSchedule();
  const canWrite = useCanWrite();
  const canSchedule = useCanSchedule();

  // Passive listener: any schedule.* WS event invalidates the orders cache
  // so the table refreshes once the worker finishes draining its queue.
  useScheduleWs();

  const handleNewOrder = useCallback(() => {
    setEditingOrder(undefined);
    setModalOpen(true);
  }, []);

  const handleEdit = useCallback((order: Order) => {
    setEditingOrder(order);
    setModalOpen(true);
  }, []);

  const handleSchedule = useCallback(() => {
    triggerSchedule.mutate(undefined, {
      onSuccess: (res) => {
        toast.success('排程已啟動', { description: res.message });
      },
      onError: (err) => {
        toast.error('排程啟動失敗', { description: err.message });
      },
    });
  }, [triggerSchedule]);

  return (
    <>
      <Header title="訂單列表" />

      <div className="px-6 py-6 space-y-5">
        <div className="flex items-center gap-2">
          {canWrite && (
            <Button onClick={handleNewOrder} size="sm">
              <Plus className="mr-1.5 h-4 w-4" />
              新增訂單
            </Button>
          )}
          {canSchedule && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleSchedule}
              disabled={triggerSchedule.isPending}
            >
              <Calendar className="mr-1.5 h-4 w-4" />
              觸發排程器
            </Button>
          )}
        </div>

        <OrderFilters />

        <OrderTable onEdit={handleEdit} />
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
