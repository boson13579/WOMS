import { Plus } from 'lucide-react';
import { useCallback, useState } from 'react';

import { Header } from '@/components/layout/Header';
import { Button } from '@/components/ui/button';
import { useCanWrite } from '@/lib/auth';

import { useTriggerSchedule } from '../api/orders';
import { useScheduleWs } from '../hooks/useScheduleWs';
import type { Order } from '../types';

import { OrderFilters } from './OrderFilters';
import { OrderModal } from './OrderModal';
import { OrderTable } from './OrderTable';

export function OrdersPage(): JSX.Element {
  const [modalOpen, setModalOpen] = useState(false);
  const [editingOrder, setEditingOrder] = useState<Order | undefined>(undefined);

  const [scheduleCompoundId, setScheduleCompoundId] = useState<string | null>(null);
  const triggerSchedule = useTriggerSchedule();
  const canWrite = useCanWrite();

  useScheduleWs(scheduleCompoundId);

  const handleNewOrder = useCallback(() => {
    setEditingOrder(undefined);
    setModalOpen(true);
  }, []);

  const handleEdit = useCallback((order: Order) => {
    setEditingOrder(order);
    setModalOpen(true);
  }, []);

  const handleSchedule = useCallback(
    (order: Order) => {
      triggerSchedule.mutate(order, {
        onSuccess: (res) => {
          setScheduleCompoundId(res.compound_id);
          // Fall-back clear: if the WS never delivers a terminal event, drop
          // the compound id after 30 s so the toast / hook don't linger.
          setTimeout(() => {
            setScheduleCompoundId(null);
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
          {canWrite && (
            <Button onClick={handleNewOrder} size="sm">
              <Plus className="mr-1.5 h-4 w-4" />
              新增訂單
            </Button>
          )}
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
