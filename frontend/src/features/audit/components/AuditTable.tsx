/**
 * Audit events table — composes header + rows + footer pagination.
 *
 * Loading / empty / error states swap the body so the surrounding
 * card chrome doesn't flicker. Loading renders 5 skeleton rows so
 * layout doesn't jump when real data lands.
 *
 * Row expand/collapse state lives here as a `Record<eventId, boolean>`
 * keyed by the event UUID. That keeps the parent (`AuditPage`)
 * focussed on URL state (filters, page, page_size) — expanded rows
 * are an in-memory UI concern that resets on page navigation.
 */
import { AlertCircle, Loader2 } from 'lucide-react';
import { useMemo, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { useUsernames } from '@/features/users/api/useUsernames';

import type { AuditEvent, AuditEventListResponse } from '../types';

import { AuditPagination, type AuditPageSize } from './AuditPagination';
import { AuditRow } from './AuditRow';

interface AuditTableProps {
  data: AuditEventListResponse | undefined;
  isLoading: boolean;
  isError: boolean;
  isFetching: boolean;
  page: number;
  pageSize: AuditPageSize;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: AuditPageSize) => void;
  onRetry: () => void;
  onClearFilters: () => void;
}

const HEAD_COLUMN_COUNT = 5; // chevron + time + actor + action + resource
const SKELETON_KEYS = ['skel-a', 'skel-b', 'skel-c', 'skel-d', 'skel-e'] as const;

function SkeletonRow({ keyId }: { keyId: string }): JSX.Element {
  return (
    <TableRow data-testid="audit-skeleton-row">
      <TableCell key={`${keyId}-chevron`} className="w-6">
        <Skeleton className="h-4 w-4 rounded" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-4 w-40" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-4 w-24" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-4 w-28 rounded-full" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-4 w-32" />
      </TableCell>
    </TableRow>
  );
}

export function AuditTable({
  data,
  isLoading,
  isError,
  isFetching,
  page,
  pageSize,
  onPageChange,
  onPageSizeChange,
  onRetry,
  onClearFilters,
}: AuditTableProps): JSX.Element {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const actorIds = useMemo(() => {
    if (!data) return [] as string[];
    const ids = data.items.map((event) => event.user_id).filter((id): id is string => id !== null);
    return Array.from(new Set(ids));
  }, [data]);

  const usernamesQuery = useUsernames(actorIds);

  const toggle = (id: string): void => {
    setExpanded((current) => ({ ...current, [id]: !current[id] }));
  };

  const renderBody = (): JSX.Element => {
    if (isLoading) {
      return (
        <TableBody>
          {SKELETON_KEYS.map((key) => (
            <SkeletonRow key={key} keyId={key} />
          ))}
        </TableBody>
      );
    }
    if (isError) {
      return (
        <TableBody>
          <TableRow>
            <TableCell colSpan={HEAD_COLUMN_COUNT} className="py-10">
              <div
                role="alert"
                className="mx-auto flex max-w-md flex-col items-center gap-3 text-center text-sm text-destructive"
              >
                <AlertCircle className="h-5 w-5" aria-hidden="true" />
                <p>Failed to load audit events. Please retry.</p>
                <Button type="button" variant="outline" size="sm" onClick={onRetry}>
                  Retry
                </Button>
              </div>
            </TableCell>
          </TableRow>
        </TableBody>
      );
    }
    if (!data || data.items.length === 0) {
      return (
        <TableBody>
          <TableRow>
            <TableCell
              colSpan={HEAD_COLUMN_COUNT}
              className="py-12 text-center text-muted-foreground"
            >
              <div className="flex flex-col items-center gap-3">
                <p>No events match your filters.</p>
                <Button type="button" variant="outline" size="sm" onClick={onClearFilters}>
                  Clear filters
                </Button>
              </div>
            </TableCell>
          </TableRow>
        </TableBody>
      );
    }

    return (
      <TableBody>
        {data.items.map((event: AuditEvent) => (
          <AuditRow
            key={event.id}
            event={event}
            actorName={event.user_id ? (usernamesQuery.data?.[event.user_id] ?? null) : null}
            expanded={Boolean(expanded[event.id])}
            onToggle={() => {
              toggle(event.id);
            }}
            colSpan={HEAD_COLUMN_COUNT}
          />
        ))}
      </TableBody>
    );
  };

  return (
    <Card data-testid="audit-table-card">
      <CardContent className="p-0">
        <Table className="min-w-[760px]">
          <TableHeader className="bg-muted/40 text-xs uppercase">
            <TableRow>
              <TableHead className="w-6" aria-label="Expand" />
              <TableHead className="w-48">Time (UTC)</TableHead>
              <TableHead className="w-36">Actor</TableHead>
              <TableHead className="w-44">Action</TableHead>
              <TableHead>Resource</TableHead>
            </TableRow>
          </TableHeader>
          {renderBody()}
        </Table>
        {data && data.items.length > 0 ? (
          <div className="flex items-center justify-between border-t border-border px-4 py-2 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-2">
              {isFetching ? (
                <Loader2
                  className="h-3.5 w-3.5 animate-spin"
                  aria-hidden="true"
                  data-testid="audit-table-fetching"
                />
              ) : null}
              {isFetching ? 'Refreshing…' : `Loaded ${data.items.length} of ${data.total}`}
            </span>
          </div>
        ) : null}
        <AuditPagination
          page={page}
          pageSize={pageSize}
          total={data?.total ?? 0}
          onPageChange={onPageChange}
          onPageSizeChange={onPageSizeChange}
          disabled={isLoading || isError}
        />
      </CardContent>
    </Card>
  );
}
