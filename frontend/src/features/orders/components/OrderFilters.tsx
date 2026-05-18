/**
 * Filter bar for the order list: keyword search + status select.
 * Filter state lives in Zustand (useOrderStore) — React Query re-fetches
 * automatically when the query key changes.
 */
import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';

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

export function OrderFilters(): JSX.Element {
  const { status, search, setStatus, setSearch, reset } = useOrderStore();

  // Debounce the search input by 300 ms to avoid spamming the API.
  const [localSearch, setLocalSearch] = useState(search);
  useEffect(() => {
    const id = setTimeout(() => {
      setSearch(localSearch);
    }, 300);
    return () => {
      clearTimeout(id);
    };
  }, [localSearch, setSearch]);

  return (
    <div className="flex flex-wrap items-center gap-3">
      <Input
        placeholder="搜尋訂單、客戶…"
        value={localSearch}
        onChange={(e) => {
          setLocalSearch(e.target.value);
        }}
        className="w-60"
        aria-label="搜尋訂單"
      />

      <div className="flex items-center gap-1.5">
        <Label htmlFor="status-filter" className="sr-only">
          篩選狀態
        </Label>
        <Select
          id="status-filter"
          value={status ?? ''}
          onChange={(e) => {
            const val = e.target.value as OrderStatus | '';
            setStatus(val !== '' ? val : null);
          }}
        >
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </Select>
      </div>

      <Button variant="outline" size="sm" onClick={reset}>
        重設
      </Button>
    </div>
  );
}
