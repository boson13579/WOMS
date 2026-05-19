/* eslint-disable no-nested-ternary */
import {
  AlertCircle,
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  GripVertical,
  Loader2,
  PackageOpen,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { DragEvent } from 'react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useCurrentRole } from '@/lib/auth';
import { cn } from '@/lib/utils';

import { useOrders } from '../api/orders';
import { usePinScheduleOperation } from '../api/scheduleOperations';
import { useScheduleResult } from '../api/scheduleResult';
import type { Order, OrderStatus, ScheduleResult } from '../types';

interface OrdersCalendarDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'];

const STATUS_LABEL: Record<OrderStatus, string> = {
  pending: '待處理',
  scheduled: '已排程',
  in_production: '生產中',
  completed: '已完成',
  cancelled: '已取消',
};

const MAX_ITEMS_PER_DAY = 3;
const DRAG_MIME = 'application/x-smart-order';

type DraggableOrder = Pick<
  Order,
  | 'id'
  | 'order_number'
  | 'customer_name'
  | 'wafer_quantity'
  | 'requested_delivery_date'
  | 'status'
  | 'is_pinned'
  | 'pinned_production_date'
  | 'is_processing_locked'
>;

interface PendingDrop {
  order: DraggableOrder;
  targetDate: string;
}

interface ActiveOperation {
  compoundId: string;
  orderId: string;
  orderNumber: string;
  targetDate: string;
  readyToVerify: boolean;
}

function dateKey(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function startOfMonth(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function addMonths(date: Date, delta: number): Date {
  return new Date(date.getFullYear(), date.getMonth() + delta, 1);
}

function calendarDays(month: Date): Date[] {
  const first = startOfMonth(month);
  const start = new Date(first);
  start.setDate(first.getDate() - first.getDay());

  return Array.from({ length: 42 }, (_, index) => {
    const day = new Date(start);
    day.setDate(start.getDate() + index);
    return day;
  });
}

function monthLabel(date: Date): string {
  return new Intl.DateTimeFormat('zh-TW', { year: 'numeric', month: 'long' }).format(date);
}

function groupByExpectedDate(items: ScheduleResult[]): Record<string, ScheduleResult[]> {
  return items.reduce<Record<string, ScheduleResult[]>>((acc, item) => {
    if (!item.expected_delivery_date) return acc;
    acc[item.expected_delivery_date] = [...(acc[item.expected_delivery_date] ?? []), item];
    return acc;
  }, {});
}

function isTargetAfterDeadline(order: DraggableOrder, targetDate: string): boolean {
  return targetDate > order.requested_delivery_date;
}

function dragOrderFromEvent(event: DragEvent): DraggableOrder | null {
  const raw = event.dataTransfer.getData(DRAG_MIME);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as DraggableOrder;
  } catch {
    return null;
  }
}

function scheduleFailureMessage(raw: unknown): string {
  if (typeof raw !== 'object' || raw === null) {
    return '後端無法執行這次排程操作。';
  }
  const event = raw as { reason?: unknown; detail?: unknown };
  const reason = typeof event.reason === 'string' ? event.reason : 'unknown';
  const detail = typeof event.detail === 'string' ? event.detail : null;
  return detail ? `${reason}: ${detail}` : reason;
}

function OrderLine({
  order,
  dragOrder,
  canDrag,
  onDragStart,
}: {
  order: ScheduleResult;
  dragOrder?: DraggableOrder;
  canDrag: boolean;
  onDragStart: (event: DragEvent, order: DraggableOrder) => void;
}): JSX.Element {
  return (
    <div
      draggable={canDrag && Boolean(dragOrder)}
      onDragStart={(event) => {
        if (dragOrder) onDragStart(event, dragOrder);
      }}
      className={cn(
        'rounded-md border border-border/70 bg-background px-2 py-1.5 text-xs',
        canDrag && dragOrder && 'cursor-grab active:cursor-grabbing',
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1 truncate font-medium">
          {canDrag && dragOrder && <GripVertical className="h-3.5 w-3.5 shrink-0" />}
          <span className="truncate">{order.order_number}</span>
        </span>
        <Badge variant="secondary" className="shrink-0">
          {STATUS_LABEL[order.status]}
        </Badge>
      </div>
      <div className="mt-1 truncate text-muted-foreground">
        {order.customer_name} · {order.wafer_quantity.toLocaleString()} 片
      </div>
    </div>
  );
}

function UnscheduledOrderLine({
  order,
  canDrag,
  onDragStart,
}: {
  order: Order;
  canDrag: boolean;
  onDragStart: (event: DragEvent, order: DraggableOrder) => void;
}): JSX.Element {
  return (
    <div
      draggable={canDrag && !order.is_processing_locked}
      onDragStart={(event) => {
        onDragStart(event, order);
      }}
      className={cn(
        'rounded-md border border-dashed px-3 py-2 text-sm',
        canDrag && !order.is_processing_locked && 'cursor-grab active:cursor-grabbing',
        order.is_processing_locked && 'opacity-60',
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1 truncate font-medium">
          {canDrag && !order.is_processing_locked && (
            <GripVertical className="h-3.5 w-3.5 shrink-0" />
          )}
          <span className="truncate">{order.order_number}</span>
        </span>
        <Badge variant="warning" className="shrink-0">
          {STATUS_LABEL[order.status]}
        </Badge>
      </div>
      <div className="mt-1 truncate text-xs text-muted-foreground">
        {order.customer_name} · 需求日 {order.requested_delivery_date}
      </div>
    </div>
  );
}

export function OrdersCalendarDialog({
  open,
  onOpenChange,
}: OrdersCalendarDialogProps): JSX.Element {
  const [visibleMonth, setVisibleMonth] = useState(() => startOfMonth(new Date()));
  const [selectedDate, setSelectedDate] = useState(() => dateKey(new Date()));
  const [pendingDrop, setPendingDrop] = useState<PendingDrop | null>(null);
  const [activeOperation, setActiveOperation] = useState<ActiveOperation | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const role = useCurrentRole();

  const scheduleResult = useScheduleResult();
  const pinSchedule = usePinScheduleOperation();
  const pendingOrders = useOrders({
    status: 'pending',
    search: null,
    page: 1,
    page_size: 100,
    sortBy: 'requested_delivery_date',
    sortOrder: 'asc',
  });
  const scheduledOrders = useOrders({
    status: 'scheduled',
    search: null,
    page: 1,
    page_size: 100,
    sortBy: 'requested_delivery_date',
    sortOrder: 'asc',
  });

  const canReadSchedule = role !== 'viewer';
  const canManageSchedule = role === 'root' || role === 'scheduler';
  const days = useMemo(() => calendarDays(visibleMonth), [visibleMonth]);
  const grouped = useMemo(
    () => groupByExpectedDate(scheduleResult.data ?? []),
    [scheduleResult.data],
  );
  const scheduledOrderById = useMemo(
    () => new Map((scheduledOrders.data?.items ?? []).map((order) => [order.id, order])),
    [scheduledOrders.data],
  );
  const selectedItems = grouped[selectedDate] ?? [];
  const unscheduled = useMemo(
    () =>
      (pendingOrders.data?.items ?? []).filter(
        (order) => order.expected_delivery_date == null && order.status === 'pending',
      ),
    [pendingOrders.data],
  );

  const handleDragStart = useCallback((event: DragEvent, order: DraggableOrder) => {
    event.dataTransfer.setData(DRAG_MIME, JSON.stringify(order));
  }, []);

  const handleDropOnDate = useCallback(
    (event: DragEvent, targetDate: string) => {
      event.preventDefault();
      if (!canManageSchedule) return;

      const order = dragOrderFromEvent(event);
      if (!order) return;

      if (order.is_processing_locked) {
        toast.error('這筆訂單仍在排程處理中，請稍後再試。');
        return;
      }
      if (order.status !== 'pending' && order.status !== 'scheduled') {
        toast.error('只能移動 pending 或 scheduled 訂單。');
        return;
      }
      if (isTargetAfterDeadline(order, targetDate)) {
        toast.error('目標日期不能晚於客戶要求交期。');
        return;
      }
      if (order.is_pinned && order.pinned_production_date === targetDate) {
        toast.info('這筆訂單已經固定在該日期。');
        return;
      }

      setSelectedDate(targetDate);
      setOperationError(null);
      setPendingDrop({ order, targetDate });
    },
    [canManageSchedule],
  );

  const submitPendingDrop = useCallback(() => {
    if (!pendingDrop) return;
    const compoundId = crypto.randomUUID();
    setActiveOperation({
      compoundId,
      orderId: pendingDrop.order.id,
      orderNumber: pendingDrop.order.order_number,
      targetDate: pendingDrop.targetDate,
      readyToVerify: false,
    });
    pinSchedule.mutate(
      { compoundId, order: pendingDrop.order, targetDate: pendingDrop.targetDate },
      {
        onSuccess: () => {
          toast.success('已送出排程嘗試，等待排程器確認。');
          setPendingDrop(null);
        },
        onError: (error) => {
          setActiveOperation(null);
          setOperationError(error.message);
          toast.error('無法送出排程操作', { description: error.message });
        },
      },
    );
  }, [pendingDrop, pinSchedule]);

  useEffect(() => {
    if (!activeOperation) return undefined;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/api/v1/ws`);

    ws.onmessage = (event: MessageEvent<string>) => {
      let payload: unknown;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }

      if (typeof payload !== 'object' || payload === null) return;
      const scheduleEvent = payload as { type?: string; compound_id?: string };

      if (
        scheduleEvent.type === 'schedule.compound_failed' &&
        scheduleEvent.compound_id === activeOperation.compoundId
      ) {
        const message = scheduleFailureMessage(scheduleEvent);
        setOperationError(message);
        setActiveOperation(null);
        toast.error('無法執行此排程操作', { description: message });
      }

      if (
        scheduleEvent.type === 'schedule.materialized' ||
        (scheduleEvent.type === 'schedule.compound_accepted' &&
          scheduleEvent.compound_id === activeOperation.compoundId)
      ) {
        setActiveOperation((current) =>
          current?.compoundId === activeOperation.compoundId
            ? { ...current, readyToVerify: scheduleEvent.type === 'schedule.materialized' }
            : current,
        );
      }
    };

    return () => {
      ws.close();
    };
  }, [activeOperation]);

  useEffect(() => {
    if (!activeOperation?.readyToVerify || scheduledOrders.isFetching) return;

    const updatedOrder = scheduledOrders.data?.items.find(
      (order) => order.id === activeOperation.orderId,
    );
    if (
      updatedOrder?.is_pinned &&
      updatedOrder.pinned_production_date === activeOperation.targetDate
    ) {
      toast.success('排程日期已套用。');
      setActiveOperation(null);
      setOperationError(null);
      return;
    }

    if (scheduledOrders.isSuccess) {
      const message = '後端已完成處理，但沒有將訂單固定到目標日期，可能是容量或期限不允許。';
      setOperationError(message);
      setActiveOperation(null);
      toast.error('排程操作未套用', { description: message });
    }
  }, [
    activeOperation,
    scheduledOrders.data,
    scheduledOrders.isFetching,
    scheduledOrders.isSuccess,
  ]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange} className="max-w-6xl">
      <DialogContent className="p-0">
        <DialogHeader className="border-b px-6 py-4">
          <DialogTitle className="flex items-center gap-2">
            <CalendarDays className="h-5 w-5" />
            訂單日曆
          </DialogTitle>
        </DialogHeader>

        <div className="grid gap-0 lg:grid-cols-[minmax(0,1fr)_320px]">
          <section className="min-w-0 p-4 sm:p-6">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h3 className="text-base font-semibold">{monthLabel(visibleMonth)}</h3>
                <p className="text-sm text-muted-foreground">依預估完成日顯示已排程訂單</p>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  aria-label="上一月"
                  onClick={() => {
                    setVisibleMonth((current) => addMonths(current, -1));
                  }}
                >
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    const today = new Date();
                    setVisibleMonth(startOfMonth(today));
                    setSelectedDate(dateKey(today));
                  }}
                >
                  今天
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  aria-label="下一月"
                  onClick={() => {
                    setVisibleMonth((current) => addMonths(current, 1));
                  }}
                >
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
            </div>

            {scheduleResult.isPending ? (
              <div className="flex h-96 items-center justify-center text-muted-foreground">
                <Loader2 className="mr-2 h-5 w-5 animate-spin" />
                載入日曆中...
              </div>
            ) : scheduleResult.isError || !canReadSchedule ? (
              <div className="flex h-96 items-center justify-center rounded-md border text-sm text-destructive">
                無法載入排程日曆，請確認帳號權限或稍後再試。
              </div>
            ) : (
              <>
                <div className="grid grid-cols-7 border-l border-t text-center text-xs font-medium text-muted-foreground">
                  {WEEKDAYS.map((day) => (
                    <div key={day} className="border-b border-r py-2">
                      {day}
                    </div>
                  ))}
                </div>
                <div className="grid grid-cols-7 border-l">
                  {days.map((day) => {
                    const key = dateKey(day);
                    const items = grouped[key] ?? [];
                    const isCurrentMonth = day.getMonth() === visibleMonth.getMonth();
                    const isSelected = key === selectedDate;
                    return (
                      <button
                        key={key}
                        type="button"
                        aria-label={`${key}${items.length > 0 ? `, ${items.length} orders` : ''}`}
                        className={cn(
                          'min-h-28 border-b border-r p-2 text-left align-top transition-colors hover:bg-muted/60',
                          !isCurrentMonth && 'bg-muted/30 text-muted-foreground',
                          isSelected &&
                            'bg-sky-50 ring-2 ring-inset ring-sky-500 dark:bg-sky-950/40',
                        )}
                        onClick={() => {
                          setSelectedDate(key);
                        }}
                        onDragOver={(event) => {
                          if (!canManageSchedule) return;
                          event.preventDefault();
                        }}
                        onDrop={(event) => {
                          handleDropOnDate(event, key);
                        }}
                      >
                        <div className="mb-1 flex items-center justify-between">
                          <span className="text-xs font-semibold">{day.getDate()}</span>
                          {items.length > 0 && (
                            <span className="rounded-full bg-sky-600 px-1.5 py-0.5 text-[10px] text-white">
                              {items.length}
                            </span>
                          )}
                        </div>
                        <div className="space-y-1">
                          {items.slice(0, MAX_ITEMS_PER_DAY).map((order) => {
                            const draggableOrder = scheduledOrderById.get(order.id);
                            const isDraggable =
                              canManageSchedule &&
                              Boolean(draggableOrder) &&
                              order.status === 'scheduled' &&
                              !draggableOrder?.is_processing_locked;
                            return (
                              <div
                                key={order.id}
                                draggable={isDraggable}
                                onDragStart={(event) => {
                                  if (draggableOrder) handleDragStart(event, draggableOrder);
                                }}
                                className={cn(
                                  'truncate rounded bg-sky-100 px-1.5 py-1 text-[11px] text-sky-950 dark:bg-sky-900 dark:text-sky-50',
                                  isDraggable && 'cursor-grab active:cursor-grabbing',
                                )}
                              >
                                {order.order_number}
                              </div>
                            );
                          })}
                          {items.length > MAX_ITEMS_PER_DAY && (
                            <div className="text-[11px] text-muted-foreground">
                              +{items.length - MAX_ITEMS_PER_DAY} 筆
                            </div>
                          )}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </>
            )}
          </section>

          <aside className="border-t bg-muted/20 p-4 lg:border-l lg:border-t-0">
            {pendingDrop && (
              <div className="mb-5 rounded-md border border-sky-200 bg-sky-50 p-3 text-sm dark:border-sky-900 dark:bg-sky-950/30">
                <div className="font-medium">確認排程移動</div>
                <p className="mt-1 text-muted-foreground">
                  嘗試將 {pendingDrop.order.order_number} 固定到 {pendingDrop.targetDate}。
                  排程器會檢查容量與期限，成功後日曆會自動更新。
                </p>
                <div className="mt-3 flex gap-2">
                  <Button
                    type="button"
                    size="sm"
                    onClick={submitPendingDrop}
                    disabled={pinSchedule.isPending}
                  >
                    {pinSchedule.isPending && (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    )}
                    送出嘗試
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setPendingDrop(null);
                    }}
                    disabled={pinSchedule.isPending}
                  >
                    取消
                  </Button>
                </div>
              </div>
            )}

            {activeOperation && (
              <div className="mb-5 rounded-md border bg-background p-3 text-sm text-muted-foreground">
                <div className="flex items-center gap-2">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  <span>
                    {activeOperation.orderNumber} 排程處理中，目標日期 {activeOperation.targetDate}
                  </span>
                </div>
              </div>
            )}

            {operationError && (
              <div className="mb-5 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                <div className="flex items-start gap-2">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{operationError}</span>
                </div>
              </div>
            )}

            <div className="mb-5">
              <h3 className="text-sm font-semibold">{selectedDate} 完成訂單</h3>
              <div className="mt-3 space-y-2">
                {selectedItems.length > 0 ? (
                  selectedItems.map((order) => {
                    const dragOrder = scheduledOrderById.get(order.id);
                    return (
                      <OrderLine
                        key={order.id}
                        order={order}
                        {...(dragOrder ? { dragOrder } : {})}
                        canDrag={canManageSchedule && order.status === 'scheduled'}
                        onDragStart={handleDragStart}
                      />
                    );
                  })
                ) : (
                  <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
                    這一天沒有預估完成的訂單。
                  </div>
                )}
              </div>
            </div>

            <div>
              <div className="mb-3 flex items-center gap-2">
                <PackageOpen className="h-4 w-4 text-muted-foreground" />
                <h3 className="text-sm font-semibold">未排程訂單</h3>
              </div>
              {pendingOrders.isPending ? (
                <div className="flex items-center py-6 text-sm text-muted-foreground">
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  載入中...
                </div>
              ) : pendingOrders.isError ? (
                <div className="rounded-md border border-destructive/40 p-3 text-sm text-destructive">
                  無法載入未排程訂單。
                </div>
              ) : (
                <div className="max-h-80 space-y-2 overflow-y-auto pr-1">
                  {unscheduled.length > 0 ? (
                    unscheduled.map((order) => (
                      <UnscheduledOrderLine
                        key={order.id}
                        order={order}
                        canDrag={canManageSchedule}
                        onDragStart={handleDragStart}
                      />
                    ))
                  ) : (
                    <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
                      目前沒有未排程訂單。
                    </div>
                  )}
                </div>
              )}

              <div className="mt-4 rounded-md bg-background p-3 text-xs text-muted-foreground">
                {canManageSchedule
                  ? '拖曳訂單到日期上可以嘗試固定排程；排程器會檢查容量與期限，成功後才會更新。'
                  : '你可以查看日曆，但只有 scheduler/root 可以拖曳調整排程。'}
              </div>
            </div>
          </aside>
        </div>
      </DialogContent>
    </Dialog>
  );
}
