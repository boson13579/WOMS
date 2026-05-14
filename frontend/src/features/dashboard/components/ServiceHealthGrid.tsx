/**
 * Grid wrapper for the Service Health card row.
 *
 * Owns the loading / error UX so the {@link StatusCard} below stays pure.
 * The grid renders 4 columns at xl breakpoints to match the 4 tracked
 * services (api / postgres / redis / celery) — fewer columns at smaller
 * widths.
 */
import { AlertTriangle } from 'lucide-react';

import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

import type { SystemHealthResponse } from '../types';

import { StatusCard } from './StatusCard';

interface ServiceHealthGridProps {
  data: SystemHealthResponse | undefined;
  isLoading: boolean;
  isError: boolean;
}

const SKELETON_COUNT = 4;

export function ServiceHealthGrid({
  data,
  isLoading,
  isError,
}: ServiceHealthGridProps): JSX.Element {
  if (isLoading) {
    return (
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: SKELETON_COUNT }, (_, i) => (
          // Skeleton key is intentionally the loop index; the slot itself
          // is anonymous (no row id) and one skeleton-per-position is the
          // entire stable identity we need here.
          // eslint-disable-next-line react/no-array-index-key
          <Skeleton key={i} data-testid="service-health-skeleton" className="h-32 w-full" />
        ))}
      </div>
    );
  }

  if (isError || !data) {
    return (
      <Card className="border-destructive/40">
        <CardContent className="flex items-start gap-3 p-5">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-destructive" />
          <p className="text-sm">Failed to load service health.</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {data.services.map((s) => (
        <StatusCard key={s.id} service={s} />
      ))}
    </div>
  );
}
