import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, LogOut } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { useAuthStore } from '@/features/auth/stores/authStore';

import { useTriggerSchedule } from '../api/orders';
import { useScheduleWs } from '../hooks/useScheduleWs';
import type { Order } from '../types';
import { OrderFilters } from './OrderFilters';
import { OrderModal } from './OrderModal';
import { OrderTable } from './OrderTable';

export function OrdersPage(): JSX.Element {
  const navigate = useNavigate();
  const { user, logout } = useAuthStore();

  const [modalOpen, setModalOpen] = useState(false);
  const [editingOrder, setEditingOrder] = useState<Order | undefined>(undefined);

  const [scheduleTaskId, setScheduleTaskId] = useState<string | null>(null);
  const triggerSchedule = useTriggerSchedule();

  useScheduleWs(scheduleTaskId);

  function handleNewOrder(): void {
    setEditingOrder(undefined);
    setModalOpen(true);
  }

  function handleEdit(order: Order): void {
    setEditingOrder(order);
    setModalOpen(true);
  }

  function handleSchedule(orderId: string): void {
    triggerSchedule.mutate(orderId, {
      onSuccess: (res) => {
        setScheduleTaskId(res.task_id);
        setTimeout(() => setScheduleTaskId(null), 30_000);
      },
    });
  }

  function handleLogout(): void {
    logout();
    void navigate('/login');
  }

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b bg-card px-6 py-3 flex items-center justify-between">
        <h1 className="text-lg font-semibold tracking-tight">Smart Order Management</h1>
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <span>{user?.username}</span>
          <Button variant="ghost" size="sm" onClick={handleLogout}>
            <LogOut className="mr-1.5 h-4 w-4" />
            登出
          </Button>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-6 space-y-5">
        <div className="flex items-center justify-between gap-4">
          <h2 className="text-xl font-medium">訂單列表</h2>
          <Button onClick={handleNewOrder} size="sm">
            <Plus className="mr-1.5 h-4 w-4" />
            新增訂單
          </Button>
        </div>

        <OrderFilters />

        <OrderTable onEdit={handleEdit} onSchedule={handleSchedule} />
      </main>

      <OrderModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        order={editingOrder}
      />
    </div>
  );
}
