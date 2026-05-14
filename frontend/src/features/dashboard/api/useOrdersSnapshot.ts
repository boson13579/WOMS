/**
 * `useOrdersSnapshot` — per-status order counts for the dashboard card.
 *
 * Fans out 4 parallel `GET /orders?status=X&page=1&page_size=1` calls
 * and aggregates each response's ``total`` into a single counts object.
 * The fan-out is cheap on modern HTTP/2 connections (multiplexed over
 * one TCP socket) and lets each per-status query cache independently.
 *
 * Polled every 30 s. The status counts change only on order CRUD or
 * scheduler transitions — minute-grain is plenty for a dashboard card.
 */
import { useQueries } from '@tanstack/react-query';
import { z } from 'zod';

import { useCurrentUser, useCurrentRole } from '@/lib/auth';

import {
  ORDERS_SNAPSHOT_STATUSES,
  type OrdersSnapshotCounts,
  type OrdersSnapshotStatus,
} from '../types';

import { apiFetch } from './apiFetch';

// We only consume ``total`` from the response — items are discarded.
// Zod still validates the rest of the shape so a malformed payload
// fails loud instead of silently returning 0.
const orderListResponseSchema = z.object({
  items: z.array(z.unknown()),
  total: z.number().int().min(0),
  page: z.number().int(),
  page_size: z.number().int(),
});

export const ordersSnapshotQueryKey = (status: OrdersSnapshotStatus) =>
  ['orders', 'snapshot', status] as const;

const REFETCH_INTERVAL_MS = 30_000;

async function fetchCount(status: OrdersSnapshotStatus): Promise<number> {
  const result = await apiFetch(
    `/api/v1/orders?status=${status}&page=1&page_size=1`,
    { credentials: 'include' },
    (d) => orderListResponseSchema.parse(d),
  );
  return result.total;
}

interface UseOrdersSnapshotResult {
  data: OrdersSnapshotCounts | undefined;
  isLoading: boolean;
  /**
   * True whenever any of the 4 underlying queries is in-flight, including
   * background refetches triggered by `refetch()` / `invalidateQueries`.
   * Distinct from `isLoading`, which only flips true on the very first
   * fetch (no cached data). Consumers wiring a Refresh button to a
   * spinner should aggregate on `isFetching` to stay in sync with the
   * other dashboard hooks.
   */
  isFetching: boolean;
  isSuccess: boolean;
  isError: boolean;
  error: Error | null;
}

export function useOrdersSnapshot(): UseOrdersSnapshotResult {
  const user = useCurrentUser();
  const role = useCurrentRole();
  const allowed = Boolean(user) && role !== 'viewer';

  const queries = useQueries({
    queries: ORDERS_SNAPSHOT_STATUSES.map((status) => ({
      queryKey: ordersSnapshotQueryKey(status),
      queryFn: () => fetchCount(status),
      enabled: allowed,
      refetchInterval: REFETCH_INTERVAL_MS,
      staleTime: 15_000,
    })),
  });

  // All-or-nothing semantics: if any single status query is mid-flight
  // we report ``isLoading``; if any errored we report ``isError`` so the
  // card shows a clear failure rather than rendering partial numbers.
  const isLoading = queries.some((q) => q.isLoading);
  // Distinct from ``isLoading``: stays true during background refetches
  // (Refresh button, polling tick) so the Header spinner aggregation
  // can stay accurate after the first successful load.
  const isFetching = queries.some((q) => q.isFetching);
  const isError = queries.some((q) => q.isError);
  const firstError = queries.find((q) => q.isError)?.error ?? null;
  const isSuccess = !isLoading && !isError && queries.every((q) => q.isSuccess);

  // ``data`` is only defined when every status query has resolved
  // successfully — otherwise the card should render its skeleton /
  // error state from the flags above.
  const data: OrdersSnapshotCounts | undefined = isSuccess
    ? ORDERS_SNAPSHOT_STATUSES.reduce<OrdersSnapshotCounts>(
        (acc, status, i) => {
          // ``isSuccess`` above guarantees every query has resolved with a
          // numeric ``data``. TS still narrows it to ``number | undefined``
          // because each ``UseQueryResult`` is independently typed; the
          // type check below is the spot where we collapse that.
          const value = queries[i].data;
          acc[status] = typeof value === 'number' ? value : 0;
          return acc;
        },
        { pending: 0, scheduled: 0, in_production: 0, completed: 0 },
      )
    : undefined;

  return {
    data,
    isLoading,
    isFetching,
    isSuccess,
    isError,
    error: firstError instanceof Error ? firstError : null,
  };
}
