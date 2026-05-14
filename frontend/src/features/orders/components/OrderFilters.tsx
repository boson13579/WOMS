/**
 * Filter bar for the order list: keyword search + status select.
 * Filter state lives in Zustand (useOrderStore) — React Query re-fetches
 * automatically when the query key changes.
 */
import { ChevronDown } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { useUsers } from '@/features/auth/api/users';
import { useCanWrite, useCurrentUserId } from '@/lib/auth';

import { useOrderStore } from '../stores/orderStore';
import type { OrderStatus } from '../types';

// ---------------------------------------------------------------------------
// Multi-select filter dropdown
// ---------------------------------------------------------------------------

interface MultiSelectOption {
  value: string;
  label: string;
}

interface MultiSelectFilterProps {
  options: MultiSelectOption[];
  value: string[];
  onChange: (v: string[]) => void;
  placeholder: string;
}

function MultiSelectFilter({
  options,
  value,
  onChange,
  placeholder,
}: MultiSelectFilterProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onClickOutside(e: MouseEvent): void {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', onClickOutside);
    return () => {
      document.removeEventListener('mousedown', onClickOutside);
    };
  }, []);

  function toggle(v: string): void {
    onChange(value.includes(v) ? value.filter((x) => x !== v) : [...value, v]);
  }

  return (
    <div className="relative" ref={ref}>
      <Button
        variant="outline"
        size="sm"
        type="button"
        onClick={() => {
          setOpen((o) => !o);
        }}
        className="h-9 gap-1"
      >
        {value.length > 0 ? `已選 ${value.length} 人` : placeholder}
        <ChevronDown className="h-3.5 w-3.5 opacity-50" />
      </Button>
      {open && (
        <div className="absolute left-0 z-50 mt-1 min-w-[200px] rounded-md border bg-background shadow-md">
          {options.length === 0 ? (
            <p className="px-3 py-2 text-sm text-muted-foreground">無可選項目</p>
          ) : (
            options.map((opt) => (
              <label
                key={opt.value}
                htmlFor={`ms-${opt.value}`}
                className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm hover:bg-accent"
              >
                <input
                  id={`ms-${opt.value}`}
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-primary"
                  checked={value.includes(opt.value)}
                  onChange={() => {
                    toggle(opt.value);
                  }}
                />
                {opt.label}
              </label>
            ))
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

const STATUS_OPTIONS: { label: string; value: OrderStatus | '' }[] = [
  { label: '全部狀態', value: '' },
  { label: '待處理', value: 'pending' },
  { label: '已排程', value: 'scheduled' },
  { label: '生產中', value: 'in_production' },
  { label: '已完成', value: 'completed' },
  { label: '已取消', value: 'cancelled' },
];

export function OrderFilters(): JSX.Element {
  const {
    status,
    search,
    assignedTo,
    createdBy,
    setStatus,
    setSearch,
    setAssignedTo,
    setCreatedBy,
    reset,
  } = useOrderStore();
  const canWrite = useCanWrite();
  const users = useUsers();
  const currentUserId = useCurrentUserId();

  const userOptions: MultiSelectOption[] = users.map((u) => ({
    value: u.id,
    label: u.id === currentUserId ? `自己 (${u.email ?? u.username})` : (u.email ?? u.username),
  }));

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

      {canWrite && (
        <>
          <MultiSelectFilter
            options={userOptions}
            value={assignedTo}
            onChange={setAssignedTo}
            placeholder="全部負責人"
          />
          <MultiSelectFilter
            options={userOptions}
            value={createdBy}
            onChange={setCreatedBy}
            placeholder="全部建立者"
          />
        </>
      )}

      <Button variant="outline" size="sm" onClick={reset}>
        重設
      </Button>
    </div>
  );
}
