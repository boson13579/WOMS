/**
 * RedEndpointsTable — top-10 by traffic, surfaces hot endpoints.
 *
 * Reads the ``by_endpoint`` slice of the RED response and renders a
 * shadcn ``Table``. Path / method are split out of the backend's
 * synthesised ``"{METHOD} {PATH}"`` label so we can style the method
 * chip independently. P99 column hides at ``<md`` to keep the row
 * readable on narrow viewports.
 */
import { AlertTriangle } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
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
import { cn } from '@/lib/utils';

import type { EndpointStat } from '../types';

interface RedEndpointsTableProps {
  data: EndpointStat[] | undefined;
  isLoading: boolean;
  isError: boolean;
  /** Window in seconds — used in the empty-state copy. */
  windowSeconds: number;
}

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' | 'OTHER';

const METHOD_VARIANT: Record<Method, 'info' | 'secondary' | 'warning' | 'destructive'> = {
  GET: 'info',
  POST: 'secondary',
  PUT: 'warning',
  PATCH: 'warning',
  DELETE: 'destructive',
  OTHER: 'secondary',
};

function parseEndpoint(label: string): { method: Method; path: string } {
  // ``String.prototype.split`` always returns a non-empty array even for an
  // empty string, so ``head`` is always defined here.
  const [head, ...rest] = label.split(' ');
  const path = rest.join(' ');
  const upper = head.toUpperCase();
  const method: Method = (
    ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(upper) ? upper : 'OTHER'
  ) as Method;
  return { method, path: path || label };
}

function formatWindow(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

export function RedEndpointsTable({
  data,
  isLoading,
  isError,
  windowSeconds,
}: RedEndpointsTableProps): JSX.Element {
  if (isLoading) {
    return (
      <Card>
        <CardContent className="p-5">
          <Skeleton data-testid="red-endpoints-skeleton" className="h-48 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (isError || !data) {
    return (
      <Card className="border-destructive/40">
        <CardContent className="flex items-start gap-3 p-5">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
          <p className="text-sm">Failed to load endpoint stats.</p>
        </CardContent>
      </Card>
    );
  }

  if (data.length === 0) {
    return (
      <Card>
        <CardContent className="p-5">
          <p className="text-sm text-muted-foreground">
            No traffic in the last {formatWindow(windowSeconds)}.
          </p>
        </CardContent>
      </Card>
    );
  }

  // Backend already sorts DESC by count; we re-sort defensively in case a
  // future endpoint variant changes ordering.
  const rows = [...data].sort((a, b) => b.count - a.count).slice(0, 10);

  return (
    <Card>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[80px]">Method</TableHead>
                <TableHead>Path</TableHead>
                <TableHead className="text-right">Count</TableHead>
                <TableHead className="text-right">Err %</TableHead>
                <TableHead className="text-right">P50</TableHead>
                <TableHead className="text-right">P95</TableHead>
                <TableHead className="hidden text-right md:table-cell">P99</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => {
                const { method, path } = parseEndpoint(row.endpoint);
                const hasErrors = row.error_pct > 0;
                return (
                  <TableRow key={row.endpoint}>
                    <TableCell>
                      <Badge variant={METHOD_VARIANT[method]} className="font-mono text-[10px]">
                        {method}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{path}</TableCell>
                    <TableCell className="text-right tabular-nums">{row.count}</TableCell>
                    <TableCell
                      className={cn(
                        'text-right tabular-nums',
                        hasErrors ? 'text-destructive' : 'text-muted-foreground',
                      )}
                    >
                      {row.error_pct.toFixed(1)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{row.p50_ms} ms</TableCell>
                    <TableCell className="text-right tabular-nums">{row.p95_ms} ms</TableCell>
                    <TableCell className="hidden text-right tabular-nums md:table-cell">
                      {row.p99_ms} ms
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
