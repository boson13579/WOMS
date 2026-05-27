/**
 * Paginated order list table.
 * Exposes onEdit callback so the parent page can manage modal open state.
 */
import { ArrowDown, ArrowUp, ArrowUpDown, Ban, Loader2, Lock, Pencil, Trash2 } from 'lucide-react';
import { useMemo, type ReactNode } from 'react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { useUsernames } from '@/features/users/api/useUsernames';
import { useCanWrite, useCurrentRole, useCurrentUserId } from '@/lib/auth';
import { toastApiError } from '@/lib/toastApiError';
import { cn } from '@/lib/utils';

import { useCancelOrder, useDeleteOrder, useOrders } from '../api/orders';
import { useOrderStore } from '../stores/orderStore';
import type { Order, OrderStatus, SortField } from '../types';

// ---------------------------------------------------------------------------
// Status badge helpers
// ---------------------------------------------------------------------------

const STATUS_LABEL: Record<OrderStatus, string> = {
  pending: '待處理',
  in_production: '生產中',
  scheduled: '已排程',
  completed: '已完成',
  cancelled: '已取消',
};

type BadgeVariant = 'default' | 'info' | 'success' | 'warning' | 'destructive' | 'secondary';

const STATUS_VARIANT: Record<OrderStatus, BadgeVariant> = {
  pending: 'warning',
  scheduled: 'info',
  in_production: 'default',
  completed: 'success',
  cancelled: 'destructive',
};

// ---------------------------------------------------------------------------
// Sortable column header
// ---------------------------------------------------------------------------

interface SortableHeadProps {
  field: SortField;
  sortBy: SortField;
  sortOrder: 'asc' | 'desc';
  onSort: (f: SortField) => void;
  className?: string;
  testId?: string;
  children: ReactNode;
}

function SortableHead({
  field,
  sortBy,
  sortOrder,
  onSort,
  className,
  testId,
  children,
}: SortableHeadProps): JSX.Element {
  const active = sortBy === field;
  const ActiveIcon = sortOrder === 'asc' ? ArrowUp : ArrowDown;
  const Icon = active ? ActiveIcon : ArrowUpDown;
  return (
    <TableHead
      className={cn('cursor-pointer select-none', className)}
      data-testid={testId}
      onClick={() => {
        onSort(field);
      }}
    >
      <span className="inline-flex items-center gap-1">
        {children}
        <Icon className={cn('h-3 w-3', !active && 'opacity-40')} />
      </span>
    </TableHead>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface OrderTableProps {
  onEdit: (order: Order) => void;
}

export function OrderTable({ onEdit }: OrderTableProps): JSX.Element {
  const { status, search, assignedTo, createdBy, page, sortBy, sortOrder, setPage, setSort } =
    useOrderStore();
  const PAGE_SIZE = 20;

  const { data, isPending, isError } = useOrders({
    status,
    search: search || null,
    assignedTo,
    createdBy,
    page,
    page_size: PAGE_SIZE,
    sortBy,
    sortOrder,
  });

  const deleteMutation = useDeleteOrder();
  const cancelMutation = useCancelOrder();
  const canWrite = useCanWrite();
  const role = useCurrentRole();
  const currentUserId = useCurrentUserId();

  const userIds = useMemo(() => {
    if (!data) return [];
    const ids = data.items
      .flatMap((o) => [o.assigned_to, o.created_by])
      .filter(Boolean) as string[];
    return Array.from(new Set(ids));
  }, [data]);
  const { data: usernamesMap } = useUsernames(userIds);

  function userDisplay(userId: string | null): string {
    if (!userId) return '—';
    if (userId === currentUserId) return '自己';
    const username = usernamesMap?.[userId];
    if (username == null) return '—';
    return username;
  }

  function canEditOrder(order: Order): boolean {
    if (!canWrite) return false;
    if (role === 'order_manager') return order.created_by === currentUserId;
    return true;
  }

  function handleDelete(order: Order): void {
    // eslint-disable-next-line no-alert
    if (!window.confirm(`確定要刪除訂單 ${order.order_number}？`)) return;
    // Backend DELETE is async for non-cancelled orders: it locks the row and
    // enqueues a worker compound that performs the soft-delete on accept. The
    // row only leaves the list once the worker finishes, so the toast must not
    // claim the order is already gone.
    deleteMutation.mutate(order.id, {
      onSuccess: () => {
        toast.success('刪除請求已送出', {
          description: `訂單 ${order.order_number} 排程處理完成後會從列表移除。`,
        });
      },
      onError: (err) => {
        toastApiError('刪除訂單失敗', err);
      },
    });
  }

  function handleCancel(order: Order): void {
    // eslint-disable-next-line no-alert
    if (!window.confirm(`確定要取消訂單 ${order.order_number}？`)) return;
    // Backend POST /cancel only locks the row and enqueues a worker compound;
    // the status flips to cancelled when the worker accepts, not on this
    // response. Word the toast as a queued request, not a completed change.
    cancelMutation.mutate(order.id, {
      onSuccess: () => {
        toast.success('取消請求已送出', {
          description: `訂單 ${order.order_number} 排程處理完成後狀態會更新為已取消。`,
        });
      },
      onError: (err) => {
        toastApiError('取消訂單失敗', err);
      },
    });
  }

  if (isPending) {
    return (
      <div className="flex items-center justify-center py-20 text-muted-foreground">
        <Loader2 className="mr-2 h-5 w-5 animate-spin" />
        載入中…
      </div>
    );
  }

  if (isError) {
    return (
      <div className="py-20 text-center text-sm text-destructive">載入失敗，請重新整理頁面。</div>
    );
  }

  const totalPages = Math.ceil(data.total / PAGE_SIZE);

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader className="bg-muted/40 text-xs uppercase">
              <TableRow>
                <SortableHead
                  field="order_number"
                  sortBy={sortBy}
                  sortOrder={sortOrder}
                  onSort={setSort}
                  className="w-36"
                >
                  訂單編號
                </SortableHead>
                <SortableHead
                  field="customer_name"
                  sortBy={sortBy}
                  sortOrder={sortOrder}
                  onSort={setSort}
                  testId="orders-sort-customer-name"
                >
                  客戶
                </SortableHead>
                <SortableHead
                  field="wafer_quantity"
                  sortBy={sortBy}
                  sortOrder={sortOrder}
                  onSort={setSort}
                  className="hidden sm:table-cell w-24 text-right"
                >
                  晶圓數量
                </SortableHead>
                <TableHead className="w-28">狀態</TableHead>
                <TableHead className="hidden md:table-cell">負責人</TableHead>
                <TableHead className="hidden lg:table-cell">建立者</TableHead>
                <SortableHead
                  field="requested_delivery_date"
                  sortBy={sortBy}
                  sortOrder={sortOrder}
                  onSort={setSort}
                  className="hidden sm:table-cell w-32"
                >
                  要求交貨日
                </SortableHead>
                <TableHead className="hidden md:table-cell w-32">預計交貨日</TableHead>
                <TableHead className="w-36 text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={9}
                    className="py-12 text-center text-muted-foreground"
                    data-testid="orders-empty-state"
                  >
                    沒有符合條件的訂單。
                  </TableCell>
                </TableRow>
              ) : (
                data.items.map((order) => (
                  <TableRow key={order.id}>
                    <TableCell className="font-mono text-xs">
                      <span className="inline-flex items-center gap-1">
                        {order.order_number}
                        {order.is_processing_locked && (
                          <Lock className="h-3 w-3 text-muted-foreground" aria-label="排程處理中" />
                        )}
                      </span>
                    </TableCell>
                    <TableCell className="font-medium">{order.customer_name}</TableCell>
                    <TableCell className="hidden sm:table-cell text-right">
                      {order.wafer_quantity.toLocaleString()}
                    </TableCell>
                    <TableCell>
                      <Badge variant={STATUS_VARIANT[order.status]}>
                        {STATUS_LABEL[order.status]}
                      </Badge>
                    </TableCell>
                    <TableCell className="hidden md:table-cell text-sm text-muted-foreground">
                      {userDisplay(order.assigned_to)}
                    </TableCell>
                    <TableCell className="hidden lg:table-cell text-sm text-muted-foreground">
                      {userDisplay(order.created_by)}
                    </TableCell>
                    <TableCell className="hidden sm:table-cell text-sm text-muted-foreground">
                      {new Date(order.requested_delivery_date).toLocaleDateString('zh-TW')}
                    </TableCell>
                    <TableCell className="hidden md:table-cell text-sm text-muted-foreground">
                      {order.expected_delivery_date
                        ? new Date(order.expected_delivery_date).toLocaleDateString('zh-TW')
                        : '—'}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center justify-end gap-1">
                        {canEditOrder(order) &&
                          (() => {
                            // Mirror backend constraints:
                            //   PATCH /orders/{id}        → 422 if status ∈ {in_production, completed, cancelled}
                            //   POST  /orders/{id}/cancel → 409 unless status === 'scheduled'
                            //   DELETE /orders/{id}       → no status guard (only is_processing_locked)
                            const isCancelled = order.status === 'cancelled';
                            const isCompleted = order.status === 'completed';
                            const isInProduction = order.status === 'in_production';
                            const isScheduled = order.status === 'scheduled';
                            const isImmutable = isInProduction || isCompleted || isCancelled;
                            const locked = order.is_processing_locked;
                            function statusBlockedTitle(): string | null {
                              if (isInProduction) return '訂單生產中，無法編輯或取消';
                              if (isCompleted) return '訂單已完成，無法編輯或取消';
                              if (isCancelled) return '訂單已取消，無法編輯或取消';
                              return null;
                            }
                            function editButtonTitle(): string {
                              const blocked = statusBlockedTitle();
                              if (blocked) return blocked;
                              if (locked) return '排程處理中，請稍候';
                              return '編輯';
                            }
                            function cancelButtonTitle(): string {
                              const blocked = statusBlockedTitle();
                              if (blocked) return blocked;
                              if (!isScheduled) return '只有已排程的訂單可以取消';
                              if (locked) return '排程處理中，請稍候';
                              return '取消訂單';
                            }
                            function deleteButtonTitle(): string {
                              if (locked) return '排程處理中，請稍候';
                              return '刪除';
                            }
                            return (
                              <>
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  onClick={() => {
                                    onEdit(order);
                                  }}
                                  title={editButtonTitle()}
                                  disabled={locked || isImmutable}
                                  data-testid="orders-edit-button"
                                >
                                  <Pencil className="h-4 w-4" />
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  onClick={() => {
                                    handleCancel(order);
                                  }}
                                  title={cancelButtonTitle()}
                                  disabled={cancelMutation.isPending || locked || !isScheduled}
                                  data-testid="orders-cancel-button"
                                >
                                  <Ban className="h-4 w-4" />
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  onClick={() => {
                                    handleDelete(order);
                                  }}
                                  title={deleteButtonTitle()}
                                  disabled={deleteMutation.isPending || locked}
                                  className="text-destructive hover:text-destructive"
                                  data-testid="orders-delete-button"
                                >
                                  <Trash2 className="h-4 w-4" />
                                </Button>
                              </>
                            );
                          })()}
                      </div>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {totalPages > 1 && (
        <div className="flex items-center justify-between text-sm text-muted-foreground">
          <span>
            共 {data.total} 筆，第 {page} / {totalPages} 頁
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setPage(page - 1);
              }}
              disabled={page <= 1}
            >
              上一頁
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setPage(page + 1);
              }}
              disabled={page >= totalPages}
            >
              下一頁
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
