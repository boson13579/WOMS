/**
 * Filter bar for the order list: keyword search + status select +
 * assignee/creator pickers. Filter state lives in Zustand (useOrderStore) —
 * React Query re-fetches automatically when the query key changes.
 */
import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { useAssignableUsers, type UserOption } from '@/features/auth/api/users';
import { useCanWrite } from '@/lib/auth';

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

function findUserIdByInput(users: UserOption[], input: string): string | null {
  const v = input.trim().toLowerCase();
  if (!v) return null;
  const matched = users.find((u) => u.username.toLowerCase() === v || u.email?.toLowerCase() === v);
  return matched?.id ?? null;
}

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
  const users = useAssignableUsers();
  const canUseUserFilters = useCanWrite();

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

  // Local text for the assignee / creator inputs — only push to the store when
  // the typed value resolves to a real user (or is cleared). Initial value uses
  // the cached user list if available; the hydration effect below covers the
  // case where the list is still loading at mount but the store carries a
  // pre-existing filter (e.g. after navigating away and returning).
  const [assigneeInput, setAssigneeInput] = useState(
    () => users.find((u) => u.id === assignedTo[0])?.username ?? '',
  );
  const [creatorInput, setCreatorInput] = useState(
    () => users.find((u) => u.id === createdBy[0])?.username ?? '',
  );
  const [didHydrate, setDidHydrate] = useState(users.length > 0);

  useEffect(() => {
    if (didHydrate || users.length === 0) return;
    setDidHydrate(true);
    // Only fill inputs that haven't been touched, so we don't clobber a user
    // mid-type if the user list resolves late.
    setAssigneeInput((prev) => {
      if (prev !== '') return prev;
      return users.find((u) => u.id === assignedTo[0])?.username ?? '';
    });
    setCreatorInput((prev) => {
      if (prev !== '') return prev;
      return users.find((u) => u.id === createdBy[0])?.username ?? '';
    });
  }, [users, assignedTo, createdBy, didHydrate]);

  useEffect(() => {
    const id = setTimeout(() => {
      // Don't touch the store until the user list has loaded — otherwise a
      // slow `/users/assignable` response would race the debounce timer and
      // we'd treat the still-empty input as "user cleared the filter",
      // wiping a pre-existing assignedTo on mount.
      if (users.length === 0) return;
      const userId = findUserIdByInput(users, assigneeInput);
      if (assigneeInput.trim() === '') {
        if (assignedTo.length > 0) setAssignedTo([]);
      } else if (userId && assignedTo[0] !== userId) {
        setAssignedTo([userId]);
      }
    }, 300);
    return () => {
      clearTimeout(id);
    };
  }, [assigneeInput, users, assignedTo, setAssignedTo]);

  useEffect(() => {
    const id = setTimeout(() => {
      if (users.length === 0) return;
      const userId = findUserIdByInput(users, creatorInput);
      if (creatorInput.trim() === '') {
        if (createdBy.length > 0) setCreatedBy([]);
      } else if (userId && createdBy[0] !== userId) {
        setCreatedBy([userId]);
      }
    }, 300);
    return () => {
      clearTimeout(id);
    };
  }, [creatorInput, users, createdBy, setCreatedBy]);

  const handleReset = (): void => {
    setLocalSearch('');
    setAssigneeInput('');
    setCreatorInput('');
    reset();
  };

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

      {canUseUserFilters && (
        <>
          <Input
            list="order-filter-users-datalist"
            placeholder="搜尋負責人姓名…"
            value={assigneeInput}
            onChange={(e) => {
              setAssigneeInput(e.target.value);
            }}
            className="w-52"
            aria-label="搜尋負責人"
            autoComplete="off"
          />

          <Input
            list="order-filter-users-datalist"
            placeholder="搜尋建立者姓名…"
            value={creatorInput}
            onChange={(e) => {
              setCreatorInput(e.target.value);
            }}
            className="w-52"
            aria-label="搜尋建立者"
            autoComplete="off"
          />

          {/* Shared datalist for both assignee and creator inputs. */}
          <datalist id="order-filter-users-datalist">
            {users.map((u) => (
              <option key={u.id} value={u.username}>
                {u.email ?? ''}
              </option>
            ))}
          </datalist>
        </>
      )}

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

      <Button variant="outline" size="sm" onClick={handleReset}>
        重設
      </Button>
    </div>
  );
}
