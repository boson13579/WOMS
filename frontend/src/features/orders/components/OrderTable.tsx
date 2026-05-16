/**
 * Paginated order list table.
 * Exposes onEdit / onSchedule callbacks so the parent page can manage
 * modal open state and schedule task IDs without prop-drilling.
 */
import { ArrowDown, ArrowUp, ArrowUpDown, Calendar, Loader2, Pencil, Trash2 } from 'lucide-react';
import type { ReactNode } from 'react';

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
import { useCanWrite } from '@/lib/auth';
import { cn } from '@/lib/utils';

import { useDeleteOrder, useOrders } from '../api/orders';
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
  children: ReactNode;
}

function SortableHead({
  field,
  sortBy,
  sortOrder,
  onSort,
  className,
  children,
}: SortableHeadProps): JSX.Element {
  const active = sortBy === field;
  const ActiveIcon = sortOrder === 'asc' ? ArrowUp : ArrowDown;
  const Icon = active ? ActiveIcon : ArrowUpDown;
  return (
    <TableHead
      className={cn('cursor-pointer select-none', className)}
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
  onSchedule: (orderId: string) => void;
}

export function OrderTable({ onEdit, onSchedule }: OrderTableProps): JSX.Element {
  const { status, search, page, sortBy, sortOrder, setPage, setSort } = useOrderStore();
  const PAGE_SIZE = 20;

  const { data, isPending, isError } = useOrders({
    status,
    search: search || null,
    page,
    page_size: PAGE_SIZE,
    sortBy,
    sortOrder,
  });

  const deleteMutation = useDeleteOrder();
  const canWrite = useCanWrite();

  function handleDelete(order: Order): void {
    // eslint-disable-next-line no-alert
    if (!window.confirm(`確定要刪除訂單 ${order.order_number}？`)) return;
    deleteMutation.mutate(order.id);
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
                  <TableCell colSpan={7} className="py-12 text-center text-muted-foreground">
                    沒有符合條件的訂單。
                  </TableCell>
                </TableRow>
              ) : (
                data.items.map((order) => (
                  <TableRow key={order.id}>
                    <TableCell className="font-mono text-xs">{order.order_number}</TableCell>
                    <TableCell className="font-medium">{order.customer_name}</TableCell>
                    <TableCell className="hidden sm:table-cell text-right">
                      {order.wafer_quantity.toLocaleString()}
                    </TableCell>
                    <TableCell>
                      <Badge variant={STATUS_VARIANT[order.status]}>
                        {STATUS_LABEL[order.status]}
                      </Badge>
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
                        {canWrite && (
                          <>
                            <Button
                              variant="ghost"
                              size="icon"
                              onClick={() => {
                                onEdit(order);
                              }}
                              title="編輯"
                            >
                              <Pencil className="h-4 w-4" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              onClick={() => {
                                onSchedule(order.id);
                              }}
                              title="觸發排程"
                            >
                              <Calendar className="h-4 w-4" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="icon"
                              onClick={() => {
                                handleDelete(order);
                              }}
                              title="刪除"
                              disabled={deleteMutation.isPending}
                              className="text-destructive hover:text-destructive"
                            >
                              <Trash2 className="h-4 w-4" />
                            </Button>
                          </>
                        )}
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
