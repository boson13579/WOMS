import { CalendarDays, Plus, RefreshCw } from 'lucide-react';
import { useCallback, useState } from 'react';
import { toast } from 'sonner';

import { Header } from '@/components/layout/Header';
import { Button } from '@/components/ui/button';
import { useCanSchedule, useCanWrite } from '@/lib/auth';
import { toastApiError } from '@/lib/toastApiError';

import { useTriggerSchedule } from '../api/orders';
import { useScheduleWs } from '../hooks/useScheduleWs';
import type { Order } from '../types';

import { OrderFilters } from './OrderFilters';
import { OrderModal } from './OrderModal';
import { OrdersCalendarDialog } from './OrdersCalendarDialog';
import { OrderTable } from './OrderTable';

export function OrdersPage(): JSX.Element {
  const [modalOpen, setModalOpen] = useState(false);
  const [calendarOpen, setCalendarOpen] = useState(false);
  const [editingOrder, setEditingOrder] = useState<Order | undefined>(undefined);

  const triggerSchedule = useTriggerSchedule();
  const canWrite = useCanWrite();
  const canSchedule = useCanSchedule();

  // Passive listener: any schedule.* WS event invalidates order and schedule caches.
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
        toastApiError('排程啟動失敗', err);
      },
    });
  }, [triggerSchedule]);

  return (
    <>
      <Header title="訂單列表" />

      <div className="px-6 py-6 space-y-5">
        <div className="flex flex-wrap items-center gap-2">
          {canWrite && (
            <Button onClick={handleNewOrder} size="sm">
              <Plus className="mr-1.5 h-4 w-4" />
              新增訂單
            </Button>
          )}
          {canWrite && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                setCalendarOpen(true);
              }}
            >
              <CalendarDays className="mr-1.5 h-4 w-4" />
              日曆視圖
            </Button>
          )}
          {canSchedule && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleSchedule}
              disabled={triggerSchedule.isPending}
            >
              <RefreshCw className="mr-1.5 h-4 w-4" />
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

      {calendarOpen && <OrdersCalendarDialog open={calendarOpen} onOpenChange={setCalendarOpen} />}
    </>
  );
}
