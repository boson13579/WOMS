/**
 * Top-level Audit log page.
 *
 * Owns URL state via `useSearchParams`: filters (actor, action,
 * resource_type, from, to) plus `page` and `page_size`. Filter changes
 * update the URL (replace history entries to avoid back-button
 * spam); pagination updates push new entries so users can step back
 * through prior pages.
 *
 * The page is root-only — `RoleProtectedRoute` enforces it at the
 * router layer; the hook also gates with `enabled: role === 'root'`
 * as defence-in-depth. A standalone access-denied card is rendered
 * if a non-root somehow reaches this component (e.g. via direct
 * link before the role check resolves).
 */
import { ShieldCheck } from 'lucide-react';
import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';

import { Header } from '@/components/layout/Header';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useCurrentRole } from '@/lib/auth';

import { useAuditEvents } from '../api/useAuditEvents';
import { auditResourceTypeSchema, type AuditFiltersState, type AuditResourceType } from '../types';

import { AuditFilters } from './AuditFilters';
import { AUDIT_PAGE_SIZE_OPTIONS, type AuditPageSize } from './AuditPagination';
import { AuditTable } from './AuditTable';

const DEFAULT_PAGE_SIZE: AuditPageSize = 20;

function parsePageSize(raw: string | null): AuditPageSize {
  const n = Number(raw);
  if (AUDIT_PAGE_SIZE_OPTIONS.includes(n as AuditPageSize)) return n as AuditPageSize;
  return DEFAULT_PAGE_SIZE;
}

function parsePage(raw: string | null): number {
  const n = Number(raw);
  return Number.isFinite(n) && n >= 1 ? Math.floor(n) : 1;
}

function parseResourceType(raw: string | null): AuditResourceType | undefined {
  if (!raw) return undefined;
  const parsed = auditResourceTypeSchema.safeParse(raw);
  return parsed.success ? parsed.data : undefined;
}

export function AuditPage(): JSX.Element {
  const role = useCurrentRole();
  const [searchParams, setSearchParams] = useSearchParams();

  const filters: AuditFiltersState = useMemo(
    () => ({
      actorId: searchParams.get('actor_id') ?? undefined,
      action: searchParams.get('action') ?? undefined,
      resourceType: parseResourceType(searchParams.get('resource_type')),
      fromDate: searchParams.get('from') ?? undefined,
      toDate: searchParams.get('to') ?? undefined,
    }),
    [searchParams],
  );
  const page = parsePage(searchParams.get('page'));
  const pageSize = parsePageSize(searchParams.get('page_size'));

  const eventsQuery = useAuditEvents({ ...filters, page, pageSize });

  const updateSearch = useCallback(
    (
      mutate: (params: URLSearchParams) => void,
      options?: { replace?: boolean; resetPage?: boolean },
    ): void => {
      const next = new URLSearchParams(searchParams);
      if (options?.resetPage) {
        next.delete('page');
      }
      mutate(next);
      setSearchParams(next, { replace: options?.replace ?? false });
    },
    [searchParams, setSearchParams],
  );

  const handleFiltersChange = useCallback(
    (nextFilters: AuditFiltersState): void => {
      updateSearch(
        (params) => {
          params.delete('actor_id');
          params.delete('action');
          params.delete('resource_type');
          params.delete('from');
          params.delete('to');
          if (nextFilters.actorId) params.set('actor_id', nextFilters.actorId);
          if (nextFilters.action) params.set('action', nextFilters.action);
          if (nextFilters.resourceType) params.set('resource_type', nextFilters.resourceType);
          if (nextFilters.fromDate) params.set('from', nextFilters.fromDate);
          if (nextFilters.toDate) params.set('to', nextFilters.toDate);
        },
        { replace: true, resetPage: true },
      );
    },
    [updateSearch],
  );

  const handleClearFilters = useCallback((): void => {
    setSearchParams(new URLSearchParams(), { replace: true });
  }, [setSearchParams]);

  const handlePageChange = useCallback(
    (next: number): void => {
      updateSearch((params) => {
        if (next <= 1) params.delete('page');
        else params.set('page', String(next));
      });
    },
    [updateSearch],
  );

  const handlePageSizeChange = useCallback(
    (size: AuditPageSize): void => {
      updateSearch(
        (params) => {
          if (size === DEFAULT_PAGE_SIZE) params.delete('page_size');
          else params.set('page_size', String(size));
        },
        { resetPage: true },
      );
    },
    [updateSearch],
  );

  const handleRefresh = useCallback((): void => {
    void eventsQuery.refetch();
  }, [eventsQuery]);

  if (role !== 'root') {
    return (
      <>
        <Header title="Audit Log" />
        <div className="mx-auto max-w-2xl px-6 py-10">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-lg">
                <ShieldCheck className="h-5 w-5" aria-hidden="true" />
                Root access required
              </CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              The audit log is available only to root users.
            </CardContent>
          </Card>
        </div>
      </>
    );
  }

  return (
    <>
      <Header
        title="Audit Log"
        subtitle="Cross-resource activity feed (root only)"
        onRefresh={handleRefresh}
        refreshing={eventsQuery.isFetching}
      />
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto flex max-w-[1400px] flex-col gap-6">
          <AuditFilters
            value={filters}
            onChange={handleFiltersChange}
            onClear={handleClearFilters}
          />
          <AuditTable
            data={eventsQuery.data}
            isLoading={eventsQuery.isLoading}
            isError={eventsQuery.isError}
            isFetching={eventsQuery.isFetching}
            page={page}
            pageSize={pageSize}
            onPageChange={handlePageChange}
            onPageSizeChange={handlePageSizeChange}
            onRetry={handleRefresh}
            onClearFilters={handleClearFilters}
          />
        </div>
      </div>
    </>
  );
}
