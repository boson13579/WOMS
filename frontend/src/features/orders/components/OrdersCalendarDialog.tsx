/* eslint-disable no-nested-ternary */
import {
  AlertCircle,
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  GripVertical,
  Loader2,
  Lock,
  PackageOpen,
  Search,
  X,
  Zap,
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
import { toDailyCapacity, useScheduleCapacity } from '../api/scheduleCapacity';
import { usePinScheduleOperation } from '../api/scheduleOperations';
import { useScheduleResult } from '../api/scheduleResult';
import type { DailyAssignment, Order, OrderStatus, ScheduleResult } from '../types';

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
  | 'version_id'
>;

interface PendingMove {
  order: DraggableOrder;
  targetDate: string;
}

interface ActiveOperation {
  compoundId: string;
  targets: {
    orderId: string;
    orderNumber: string;
    targetDate: string;
  }[];
  readyToVerify: boolean;
}

interface ProductionCalendarItem extends ScheduleResult {
  productionDate: string;
  productionQuantity: number;
  cumulativeQuantity: number;
  productionState: 'in_progress' | 'complete' | 'scheduled';
}

function getProductionStateLabel(state: 'complete' | 'in_progress' | 'scheduled'): string {
  if (state === 'complete') return '已完成';
  if (state === 'in_progress') return '生產中';
  return '已排程';
}

function getProductionStateVariant(
  state: 'complete' | 'in_progress' | 'scheduled',
): 'success' | 'warning' | 'info' | 'secondary' {
  if (state === 'complete') return 'success';
  if (state === 'in_progress') return 'warning';
  return 'info';
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

function cumulativeQuantityUntil(assignments: DailyAssignment[], date: string): number {
  return assignments
    .filter((assignment) => assignment.date <= date)
    .reduce((total, assignment) => total + assignment.quantity, 0);
}

// `baseDate` is the server's production "today" anchor. When it is undefined
// (capacity query still loading / errored), every row collapses to `scheduled`
// so the UI never quietly falls back to the client clock — that fallback
// would re-introduce the timezone bug this view was rewritten to avoid.
function groupByProductionDate(
  items: ScheduleResult[],
  baseDate: string | undefined,
): Record<string, ProductionCalendarItem[]> {
  return items.reduce<Record<string, ProductionCalendarItem[]>>((acc, item) => {
    item.daily_breakdown.forEach((assignment) => {
      const cumulativeQuantity = cumulativeQuantityUntil(item.daily_breakdown, assignment.date);

      let productionState: 'complete' | 'in_progress' | 'scheduled';

      if (baseDate === undefined) {
        productionState = 'scheduled';
      } else if (assignment.date < baseDate) {
        productionState = 'complete';
      } else if (assignment.date > baseDate) {
        productionState = 'scheduled';
      } else {
        productionState = 'in_progress';
      }

      const productionItem: ProductionCalendarItem = {
        ...item,
        productionDate: assignment.date,
        productionQuantity: assignment.quantity,
        cumulativeQuantity,
        productionState,
      };
      acc[assignment.date] = [...(acc[assignment.date] ?? []), productionItem];
    });
    return acc;
  }, {});
}

function canDragScheduledOrder(
  order: ProductionCalendarItem,
  dragOrder: DraggableOrder | undefined,
  canManageSchedule: boolean,
): boolean {
  return (
    canManageSchedule &&
    order.status === 'scheduled' &&
    order.productionState !== 'in_progress' &&
    dragOrder !== undefined &&
    !dragOrder.is_processing_locked
  );
}

function capacityTone(remaining: number, dailyCapacity: number): string {
  if (remaining <= 0) return 'text-red-600 dark:text-red-400';
  if (remaining <= dailyCapacity / 2) return 'text-amber-600 dark:text-amber-400';
  return 'text-emerald-600 dark:text-emerald-400';
}

function isTargetAfterDeadline(order: DraggableOrder, targetDate: string): boolean {
  return targetDate > order.requested_delivery_date;
}

function dragOrdersFromEvent(event: DragEvent): DraggableOrder[] {
  const raw = event.dataTransfer.getData(DRAG_MIME);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as { orders?: DraggableOrder[] } | DraggableOrder;
    if ('orders' in parsed && Array.isArray(parsed.orders)) return parsed.orders;
    return [parsed as DraggableOrder];
  } catch {
    return [];
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
  selected,
  onSelectedChange,
}: {
  order: ProductionCalendarItem;
  dragOrder?: DraggableOrder;
  canDrag: boolean;
  onDragStart: (event: DragEvent, order: DraggableOrder) => void;
  selected: boolean;
  onSelectedChange: (orderId: string, selected: boolean) => void;
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
          {canDrag && dragOrder && (
            <input
              type="checkbox"
              aria-label={`Select ${order.order_number}`}
              checked={selected}
              onChange={(event) => {
                onSelectedChange(order.id, event.target.checked);
              }}
              onClick={(event) => {
                event.stopPropagation();
              }}
              className="h-3.5 w-3.5 shrink-0"
            />
          )}
          {canDrag && dragOrder && <GripVertical className="h-3.5 w-3.5 shrink-0" />}
          <span className="truncate">{order.order_number}</span>
          {(order.productionState === 'in_progress' || dragOrder?.is_processing_locked) && (
            <Lock
              aria-label="處理鎖定中"
              className="h-3.5 w-3.5 text-amber-600 dark:text-amber-400 shrink-0"
            />
          )}
        </span>
        <Badge variant={getProductionStateVariant(order.productionState)} className="shrink-0">
          {getProductionStateLabel(order.productionState)}
        </Badge>
      </div>
      <div className="mt-1 truncate text-muted-foreground">
        {order.customer_name} · 今日 {order.productionQuantity.toLocaleString()} / 累計{' '}
        {order.cumulativeQuantity.toLocaleString()} / {order.wafer_quantity.toLocaleString()}
      </div>
    </div>
  );
}

function UnscheduledOrderLine({
  order,
  canDrag,
  onDragStart,
  selected,
  onSelectedChange,
}: {
  order: Order;
  canDrag: boolean;
  onDragStart: (event: DragEvent, order: DraggableOrder) => void;
  selected: boolean;
  onSelectedChange: (orderId: string, selected: boolean) => void;
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
            <input
              type="checkbox"
              aria-label={`Select ${order.order_number}`}
              checked={selected}
              onChange={(event) => {
                onSelectedChange(order.id, event.target.checked);
              }}
              onClick={(event) => {
                event.stopPropagation();
              }}
              className="h-3.5 w-3.5 shrink-0"
            />
          )}
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
  const [pendingMoves, setPendingMoves] = useState<PendingMove[]>([]);
  const [activeOperation, setActiveOperation] = useState<ActiveOperation | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [selectedOrderIds, setSelectedOrderIds] = useState<string[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const role = useCurrentRole();

  const scheduleResult = useScheduleResult();
  const scheduleCapacity = useScheduleCapacity();
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
    () => groupByProductionDate(scheduleResult.data ?? [], scheduleCapacity.data?.base_date),
    [scheduleResult.data, scheduleCapacity.data?.base_date],
  );
  const filteredGrouped = useMemo(() => {
    if (!searchQuery) return grouped;
    const query = searchQuery.toLowerCase();
    return Object.keys(grouped).reduce<Record<string, ProductionCalendarItem[]>>((acc, date) => {
      const items = grouped[date];
      const matched = items.filter(
        (item) =>
          item.order_number.toLowerCase().includes(query) ||
          item.customer_name.toLowerCase().includes(query),
      );
      if (matched.length > 0) {
        acc[date] = matched;
      }
      return acc;
    }, {});
  }, [grouped, searchQuery]);

  const dailyCapacityByDate = useMemo(() => {
    if (!scheduleCapacity.data)
      return new Map<string, { remaining: number; dailyCapacity: number }>();

    return new Map(toDailyCapacity(scheduleCapacity.data).map((entry) => [entry.date, entry]));
  }, [scheduleCapacity.data]);
  const scheduledOrderById = useMemo(
    () => new Map((scheduledOrders.data?.items ?? []).map((order) => [order.id, order])),
    [scheduledOrders.data],
  );
  const selectedItems = filteredGrouped[selectedDate] ?? [];
  const unscheduled = useMemo(
    () =>
      (pendingOrders.data?.items ?? []).filter(
        (order) => order.expected_delivery_date == null && order.status === 'pending',
      ),
    [pendingOrders.data],
  );
  const filteredUnscheduled = useMemo(() => {
    if (!searchQuery) return unscheduled;
    const query = searchQuery.toLowerCase();
    return unscheduled.filter(
      (order) =>
        order.order_number.toLowerCase().includes(query) ||
        order.customer_name.toLowerCase().includes(query),
    );
  }, [unscheduled, searchQuery]);
  const selectableOrders = useMemo(
    () => [...(scheduledOrders.data?.items ?? []), ...unscheduled],
    [scheduledOrders.data, unscheduled],
  );

  const selectedOrders = useMemo(
    () => selectableOrders.filter((order) => selectedOrderIds.includes(order.id)),
    [selectableOrders, selectedOrderIds],
  );

  const updateSelectedOrder = useCallback((orderId: string, selected: boolean) => {
    setSelectedOrderIds((current) => {
      if (selected) return current.includes(orderId) ? current : [...current, orderId];
      return current.filter((id) => id !== orderId);
    });
  }, []);

  const handleDragStart = useCallback(
    (event: DragEvent, order: DraggableOrder) => {
      const ordersToDrag = selectedOrderIds.includes(order.id) ? selectedOrders : [order];
      event.dataTransfer.setData(DRAG_MIME, JSON.stringify({ orders: ordersToDrag }));
    },
    [selectedOrderIds, selectedOrders],
  );

  const handleDropOnDate = useCallback(
    (event: DragEvent, targetDate: string) => {
      event.preventDefault();
      if (!canManageSchedule) return;

      const orders = dragOrdersFromEvent(event);
      if (orders.length === 0) return;

      if (orders.some((order) => order.is_processing_locked)) {
        toast.error('這筆訂單仍在排程處理中，請稍後再試。');
        return;
      }
      if (orders.some((order) => order.status !== 'pending' && order.status !== 'scheduled')) {
        toast.error('只能移動 pending 或 scheduled 訂單。');
        return;
      }
      if (orders.some((order) => isTargetAfterDeadline(order, targetDate))) {
        toast.error('目標日期不能晚於客戶要求交期。');
        return;
      }
      if (orders.every((order) => order.is_pinned && order.pinned_production_date === targetDate)) {
        toast.info('選取的訂單已經固定在該日期。');
        return;
      }

      setSelectedDate(targetDate);
      setOperationError(null);
      setPendingMoves((current) => {
        const droppedOrderIds = new Set(orders.map((order) => order.id));
        return [
          ...current.filter((move) => !droppedOrderIds.has(move.order.id)),
          ...orders.map((order) => ({ order, targetDate })),
        ];
      });
    },
    [canManageSchedule],
  );

  const removePendingMove = useCallback((orderId: string) => {
    setPendingMoves((current) => current.filter((move) => move.order.id !== orderId));
  }, []);

  const submitPendingMoves = useCallback(() => {
    if (pendingMoves.length === 0) return;
    const compoundId = crypto.randomUUID();
    setActiveOperation({
      compoundId,
      targets: pendingMoves.map((move) => ({
        orderId: move.order.id,
        orderNumber: move.order.order_number,
        targetDate: move.targetDate,
      })),
      readyToVerify: false,
    });
    pinSchedule.mutate(
      {
        compoundId,
        targets: pendingMoves.map((move) => ({
          order: move.order,
          targetDate: move.targetDate,
        })),
      },
      {
        onSuccess: () => {
          toast.success('已送出排程嘗試，等待排程器確認。', {
            description: (
              <div className="mt-2 space-y-1.5">
                {pendingMoves.map((move) => (
                  <div key={move.order.id} className="flex flex-col gap-0.5">
                    <span className="font-mono text-[11px] font-semibold text-foreground">
                      {move.order.order_number}
                    </span>
                    <span className="text-[10px] text-muted-foreground">
                      拖移至: {move.targetDate}
                    </span>
                  </div>
                ))}
              </div>
            ),
          });
          setPendingMoves([]);
          setSelectedOrderIds([]);
        },
        onError: (error) => {
          setActiveOperation(null);
          setOperationError(error.message);
          toast.error('無法送出排程操作', { description: error.message });
        },
      },
    );
  }, [pendingMoves, pinSchedule]);

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

    const targetByOrderId = new Map(
      activeOperation.targets.map((target) => [target.orderId, target.targetDate]),
    );
    const updatedOrders = scheduledOrders.data?.items.filter((order) =>
      targetByOrderId.has(order.id),
    );
    if (
      updatedOrders?.length === activeOperation.targets.length &&
      updatedOrders.every(
        (order) =>
          order.is_pinned && order.pinned_production_date === targetByOrderId.get(order.id),
      )
    ) {
      // One coalesced toast instead of N — bulk operations were spraying a
      // toast per affected order and competing with the on-screen pending /
      // active-operation cards that already enumerate each target.
      const { targets } = activeOperation;
      toast.success(
        targets.length === 1
          ? `訂單 ${targets[0].orderNumber} 已更改日期至 ${targets[0].targetDate}`
          : `${targets.length} 筆訂單已套用排程`,
        targets.length > 1
          ? {
              description: (
                <div className="mt-2 space-y-1.5">
                  {targets.map((target) => (
                    <div key={target.orderId} className="flex flex-col gap-0.5">
                      <span className="font-mono text-[11px] font-semibold text-foreground">
                        {target.orderNumber}
                      </span>
                      <span className="text-[10px] text-muted-foreground">
                        套用至: {target.targetDate}
                      </span>
                    </div>
                  ))}
                </div>
              ),
            }
          : undefined,
      );
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
                <p className="text-sm text-muted-foreground">依生產日期顯示已排程訂單</p>
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
                    const items = filteredGrouped[key] ?? [];
                    const capacity = dailyCapacityByDate.get(key);
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
                          <span className="flex items-center gap-1">
                            {capacity && (
                              <span
                                className={cn(
                                  'inline-flex items-center gap-0.5 text-[10px] font-medium',
                                  capacityTone(capacity.remaining, capacity.dailyCapacity),
                                )}
                                title="當日剩餘產能"
                              >
                                <Zap className="h-3 w-3" />
                                {capacity.remaining.toLocaleString()}
                              </span>
                            )}
                            {items.length > 0 && (
                              <span className="rounded-full bg-sky-600 px-1.5 py-0.5 text-[10px] text-white">
                                {items.length}
                              </span>
                            )}
                          </span>
                        </div>
                        <div className="space-y-1">
                          {items.slice(0, MAX_ITEMS_PER_DAY).map((order) => {
                            const draggableOrder = scheduledOrderById.get(order.id);
                            const isDraggable = canDragScheduledOrder(
                              order,
                              draggableOrder,
                              canManageSchedule,
                            );
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
                                {order.order_number} · {order.productionQuantity.toLocaleString()}
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
            {pendingMoves.length > 0 && (
              <div className="mb-5 rounded-md border border-sky-200 bg-sky-50 p-3 text-sm dark:border-sky-900 dark:bg-sky-950/30">
                <div className="font-medium">待送出的排程變更</div>
                <div className="mt-2 space-y-2 text-muted-foreground">
                  {pendingMoves.map((move) => (
                    <div
                      key={move.order.id}
                      className="flex items-start justify-between gap-2 border-b border-sky-100 dark:border-sky-900/50 pb-2 last:border-0 last:pb-0"
                    >
                      <div className="flex flex-col gap-0.5 min-w-0">
                        <span className="font-mono text-xs font-semibold text-foreground truncate">
                          {move.order.order_number}
                        </span>
                        <span className="text-[11px] text-muted-foreground">
                          拖移至: {move.targetDate}
                        </span>
                      </div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          removePendingMove(move.order.id);
                        }}
                        disabled={pinSchedule.isPending}
                        className="h-5 px-1.5 text-[10px] text-destructive hover:bg-destructive/10 shrink-0"
                      >
                        移除
                      </Button>
                    </div>
                  ))}
                </div>
                <div className="mt-3 flex gap-2">
                  <Button
                    type="button"
                    size="sm"
                    onClick={submitPendingMoves}
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
                      setPendingMoves([]);
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
                <div className="flex items-start gap-2">
                  <Loader2 className="mt-0.5 h-4 w-4 animate-spin shrink-0" />
                  <div className="space-y-1.5 flex-1 min-w-0">
                    <div className="font-medium text-foreground">排程處理中...</div>
                    <div className="space-y-1.5">
                      {activeOperation.targets.map((target) => (
                        <div
                          key={target.orderId}
                          className="flex flex-col gap-0.5 border-b border-border/50 pb-1.5 last:border-0 last:pb-0"
                        >
                          <span className="font-mono text-xs font-semibold text-foreground truncate">
                            {target.orderNumber}
                          </span>
                          <span className="text-[11px] text-muted-foreground">
                            拖移至: {target.targetDate}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
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
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold">{selectedDate} 生產訂單</h3>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      aria-label="搜尋訂單"
                      onClick={() => {
                        setIsSearching((prev) => !prev);
                        if (isSearching) {
                          setSearchQuery('');
                        }
                      }}
                      className="h-7 w-7 text-muted-foreground hover:text-foreground"
                    >
                      {isSearching ? <X className="h-4 w-4" /> : <Search className="h-4 w-4" />}
                    </Button>
                    {dailyCapacityByDate.get(selectedDate) && (
                      <span
                        className={cn(
                          'inline-flex items-center gap-1 text-xs font-medium',
                          capacityTone(
                            dailyCapacityByDate.get(selectedDate)?.remaining ?? 0,
                            dailyCapacityByDate.get(selectedDate)?.dailyCapacity ?? 1,
                          ),
                        )}
                      >
                        <Zap className="h-3.5 w-3.5" />
                        剩餘 {dailyCapacityByDate.get(selectedDate)?.remaining.toLocaleString()}
                      </span>
                    )}
                  </div>
                </div>

                {isSearching && (
                  <div className="relative">
                    <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
                    <input
                      type="text"
                      placeholder="輸入單號或客戶名稱..."
                      value={searchQuery}
                      onChange={(e) => {
                        setSearchQuery(e.target.value);
                      }}
                      className="w-full rounded-md border border-input bg-background pl-8 pr-3 py-1.5 text-xs ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                    />
                  </div>
                )}
              </div>
              <div className="mt-3 max-h-72 space-y-2 overflow-y-auto pr-1">
                {selectedItems.length > 0 ? (
                  selectedItems.map((order) => {
                    const dragOrder = scheduledOrderById.get(order.id);
                    return (
                      <OrderLine
                        key={order.id}
                        order={order}
                        {...(dragOrder ? { dragOrder } : {})}
                        canDrag={canDragScheduledOrder(order, dragOrder, canManageSchedule)}
                        onDragStart={handleDragStart}
                        selected={selectedOrderIds.includes(order.id)}
                        onSelectedChange={updateSelectedOrder}
                      />
                    );
                  })
                ) : (
                  <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
                    這天沒有生產訂單。
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
                  {filteredUnscheduled.length > 0 ? (
                    filteredUnscheduled.map((order) => (
                      <UnscheduledOrderLine
                        key={order.id}
                        order={order}
                        canDrag={canManageSchedule}
                        onDragStart={handleDragStart}
                        selected={selectedOrderIds.includes(order.id)}
                        onSelectedChange={updateSelectedOrder}
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
