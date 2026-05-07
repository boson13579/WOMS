/**
 * Filter bar for the order list: keyword search + status select.
 * Filter state lives in Zustand (useOrderStore) — React Query re-fetches
 * automatically when the query key changes.
 */
import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

import { useOrderStore } from '../stores/orderStore';
import type { OrderStatus } from '../types';

const STATUS_OPTIONS: { label: string; value: OrderStatus | '' }[] = [
  { label: '全部狀態', value: '' },
  { label: '待處理', value: 'pending' },
  { label: '已排程', value: 'scheduled' },
  { label: '生產中', value: 'in_production' },
  { label: '已完成', value: 'completed' },
  { label: '已取消', value: 'cancelled' },
];

const selectCls =
  'h-9 rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus:outline-none focus:ring-1 focus:ring-ring';

export function OrderFilters(): JSX.Element {
  const { status, search, setStatus, setSearch, reset } = useOrderStore();

  // Debounce the search input by 300 ms to avoid spamming the API.
  const [localSearch, setLocalSearch] = useState(search);
  useEffect(() => {
    const id = setTimeout(() => setSearch(localSearch), 300);
    return () => clearTimeout(id);
  }, [localSearch, setSearch]);

  return (
    <div className="flex flex-wrap items-center gap-3">
      <Input
        placeholder="搜尋訂單、客戶…"
        value={localSearch}
        onChange={(e) => setLocalSearch(e.target.value)}
        className="w-60"
        aria-label="搜尋訂單"
      />

      <select
        value={status ?? ''}
        onChange={(e) => setStatus((e.target.value as OrderStatus) || null)}
        className={selectCls}
        aria-label="篩選狀態"
      >
        {STATUS_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>

      <Button variant="outline" size="sm" onClick={reset}>
        重設
      </Button>
    </div>
  );
}
