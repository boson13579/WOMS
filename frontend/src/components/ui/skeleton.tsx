/**
 * shadcn/ui Skeleton — placeholder for content that hasn't loaded yet.
 *
 * Renders a pulsing rounded rectangle. Compose to mimic the final layout
 * (the dashboard's loading state stacks several skeletons in the same grid
 * as the real cards, so the UI doesn't visibly reflow when data arrives).
 */
import * as React from 'react';

import { cn } from '@/lib/utils';

export const Skeleton = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn('animate-pulse rounded-md bg-muted', className)} {...props} />
  ),
);
Skeleton.displayName = 'Skeleton';
