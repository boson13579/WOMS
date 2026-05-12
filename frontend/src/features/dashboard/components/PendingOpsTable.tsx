/**
 * Pending-ops queue snapshot.
 *
 * The backend endpoint returns the FULL queue (which can be 1000+ entries
 * during a batch update). The dashboard widget only shows the top N rows
 * — what an operator actually needs is "what's about to be processed" and
 * "how deep is the backlog right now" — and the footer carries the
 * "showing X of Y" count so the cap is visible.
 *
 * For inspecting the whole queue we'd ship a dedicated /queue page later;
 * baking that into this card would crowd the dashboard.
 */
import { AlertTriangle, Inbox, Layers } from 'lucide-react';
import { useMemo } from 'react';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

import { useUsernames } from '../api/useUsernames';
import type { PendingOpsEntry } from '../types';

const DEFAULT_TOP_N = 10;

interface PendingOpsTableProps {
  data: PendingOpsEntry[] | undefined;
  isLoading: boolean;
  isError: boolean;
  /** How many rows to surface at the top; defaults to 10. */
  topN?: number;
}

export function PendingOpsTable({
  data,
  isLoading,
  isError,
  topN = DEFAULT_TOP_N,
}: PendingOpsTableProps): JSX.Element {
  // Resolve the requester UUIDs to usernames in bulk. ``useUsernames``
  // short-circuits on an empty list so this is safe even before data
  // loads — the hook returns ``{}`` and the table renders the raw UUID
  // as a fallback below. Memoize so polling-driven re-renders don't
  // recompute the requester array on every tick.
  const requesterIds = useMemo(
    () => (data ?? []).slice(0, topN).map((e) => e.requested_by),
    [data, topN],
  );
  const usernames = useUsernames(requesterIds);

  if (isLoading) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle>Pending operations</CardTitle>
          <CardDescription>queued compounds awaiting the scheduler</CardDescription>
        </CardHeader>
        <CardContent>
          <Skeleton data-testid="pending-ops-skeleton" className="h-48 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (isError || !data) {
    return (
      <Card className="border-destructive/40">
        <CardContent className="flex items-start gap-3 p-5">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
          <p className="text-sm">Failed to load pending operations.</p>
        </CardContent>
      </Card>
    );
  }

  const total = data.length;
  const rows = data.slice(0, topN);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-3">
        <div>
          <CardTitle className="flex items-center gap-2">
            <Layers className="h-4 w-4 text-muted-foreground" aria-hidden />
            Pending operations
          </CardTitle>
          <CardDescription>
            {total === 0
              ? 'queue is currently empty'
              : `${total.toLocaleString()} pending compound${total === 1 ? '' : 's'}`}
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        {total === 0 ? (
          <div className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground">
            <Inbox className="h-4 w-4" aria-hidden />
            Queue is empty.
          </div>
        ) : (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  {/*
                   * Width tuning (see notes below the table for the breakdown):
                   *   #            48px  — rank, ≤ 4 digits
                   *   Order       192px  — fits one ORD-YYYYMMDD-NNNN with breathing room;
                   *                        multi-order compounds truncate (rare)
                   *   Action      224px  — longest case "unpin → remove → add → pin" ≈ 195px
                   *   Group        80px  — just the badge
                   *   Requested by auto — grows, holds username or fallback UUID
                   */}
                  <TableHead className="w-12">#</TableHead>
                  <TableHead className="w-48">Order</TableHead>
                  <TableHead className="w-56">Action</TableHead>
                  <TableHead className="w-20">Group</TableHead>
                  <TableHead className="text-right">Requested by</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((entry) => {
                  // Distinct order numbers in the compound — usually one for
                  // single-order flows, but batch compounds can mix several.
                  const orderNumbers = Array.from(new Set(entry.ops.map((o) => o.order_number)));
                  // Op kinds in original order — operators care that a
                  // compound is e.g. ``unpin → remove → add → pin`` (pinned
                  // PATCH) vs. ``add`` (simple create) more than the raw count.
                  const actionLabel = entry.ops.map((o) => o.op).join(' → ');
                  // Resolve UUID → username. Fall back to a truncated UUID
                  // while ``useUsernames`` is mid-flight so the column
                  // doesn't flicker between empty and populated.
                  const username = usernames.data?.[entry.requested_by];
                  const requesterDisplay = username ?? `${entry.requested_by.slice(0, 8)}…`;
                  return (
                    <TableRow key={entry.compound_id}>
                      <TableCell className="font-mono tabular-nums">{entry.rank}</TableCell>
                      <TableCell
                        className="truncate font-mono text-xs"
                        title={orderNumbers.join(', ')}
                      >
                        {orderNumbers.join(', ')}
                      </TableCell>
                      <TableCell className="truncate font-mono text-xs">{actionLabel}</TableCell>
                      <TableCell>
                        <Badge variant={entry.group === 'shrink' ? 'info' : 'success'}>
                          {entry.group}
                        </Badge>
                      </TableCell>
                      <TableCell
                        className="text-right text-xs text-muted-foreground"
                        title={entry.requested_by}
                      >
                        {requesterDisplay}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
            {total > topN ? (
              <p className="mt-3 text-xs text-muted-foreground">
                Showing {rows.length} of {total.toLocaleString()} pending compounds.
              </p>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}
