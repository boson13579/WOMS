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
                  <TableHead className="w-12">#</TableHead>
                  <TableHead>Order</TableHead>
                  <TableHead className="w-24">Group</TableHead>
                  <TableHead className="w-24">Ops</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((entry) => {
                  // Distinct order numbers in the compound — usually one for
                  // single-order flows, but batch compounds can mix several.
                  const orderNumbers = Array.from(new Set(entry.ops.map((o) => o.order_number)));
                  return (
                    <TableRow key={entry.compound_id}>
                      <TableCell className="font-mono tabular-nums">{entry.rank}</TableCell>
                      <TableCell className="truncate font-mono text-xs">
                        {orderNumbers.join(', ')}
                      </TableCell>
                      <TableCell>
                        <Badge variant={entry.group === 'shrink' ? 'info' : 'success'}>
                          {entry.group}
                        </Badge>
                      </TableCell>
                      <TableCell className="tabular-nums">{entry.op_count}</TableCell>
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
