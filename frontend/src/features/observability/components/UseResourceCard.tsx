/**
 * UseResourceCard — generic resource-utilization tile.
 *
 * Shared shell for the DB pool / Redis memory / Workers cards in the
 * USE row. The optional ``expandable`` slot lets the Workers card drop
 * a per-worker drilldown beneath the aggregate without forking a new
 * component (round-2 verifier recommendation).
 *
 * Visual rules per plan:
 *   - utilization bar: ``bg-secondary`` track, ``bg-primary`` fill;
 *     swap to ``bg-destructive`` when ratio > 0.8.
 *   - "unreachable" note when ``value === null`` (gracefully degraded
 *     section without blanking the page).
 */
import type { ReactNode } from 'react';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';

interface UseResourceCardProps {
  label: string;
  /** Big number / formatted display. ``null`` renders the unreachable state. */
  value: string | null;
  /** Optional smaller side text rendered next to the value. */
  detail?: string | undefined;
  /** Utilization ratio in ``[0, 1]``; controls bar width + colour. */
  ratio?: number | null | undefined;
  /**
   * Suffix under the bar. ``string`` renders one line; ``string[]``
   * renders each entry as its own line (e.g. one per replica pod).
   */
  caption?: string | string[] | undefined;
  /** Optional drilldown content rendered after the bar / caption. */
  expandable?: ReactNode | undefined;
  /**
   * Optional copy override for the unreachable-state row; defaults to
   * a sensible "Probe unreachable" line.
   */
  unreachableMessage?: string | undefined;
  /**
   * When ``true``, the utilization bar slot is omitted entirely (no
   * dashed placeholder either). Use for cards that have no natural
   * saturation denominator — e.g. Live Connections (no upper bound on
   * "how many dashboards are watching"), or Redis on a local docker
   * setup where ``maxmemory=0`` (no cap configured).
   */
  hideBar?: boolean;
}

const UNREACHABLE_DEFAULT = 'Probe unreachable.';

function renderUtilizationBar(
  label: string,
  clampedRatio: number | null,
  widthPct: number,
  isUnreachable: boolean,
  isHot: boolean,
): JSX.Element {
  return (
    <div
      className="h-2 w-full overflow-hidden rounded-full bg-secondary"
      role="progressbar"
      aria-valuenow={isUnreachable ? undefined : widthPct}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={`${label} utilization`}
    >
      {clampedRatio === null ? (
        <div
          data-testid="util-bar-dashed"
          className="h-full w-full bg-muted-foreground/20 [background-image:repeating-linear-gradient(45deg,transparent,transparent_4px,hsl(var(--border))_4px,hsl(var(--border))_8px)]"
        />
      ) : (
        <div
          data-testid="util-bar-fill"
          className={cn('h-full transition-all', isHot ? 'bg-destructive' : 'bg-primary')}
          style={{ width: `${widthPct}%` }}
        />
      )}
    </div>
  );
}

function renderCaptionLines(captionLines: readonly (string | undefined)[]): JSX.Element[] {
  return captionLines.map((line) => (
    <p key={line} className="text-xs text-muted-foreground">
      {line}
    </p>
  ));
}

export function UseResourceCard({
  label,
  value,
  detail,
  ratio,
  caption,
  expandable,
  unreachableMessage = UNREACHABLE_DEFAULT,
  hideBar = false,
}: Readonly<UseResourceCardProps>): JSX.Element {
  const isUnreachable = value === null;
  // Clamp ratio so a borked backend value can't paint a 200%-wide bar.
  const clampedRatio = typeof ratio === 'number' ? Math.max(0, Math.min(1, ratio)) : null;
  const widthPct = clampedRatio === null ? 0 : Math.round(clampedRatio * 100);
  const isHot = clampedRatio !== null && clampedRatio > 0.8;
  const captionLines = Array.isArray(caption) ? caption : [caption];

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{label}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-baseline justify-between gap-2">
          <span
            className={cn(
              'text-2xl font-semibold tabular-nums tracking-tight',
              isUnreachable && 'text-muted-foreground',
            )}
          >
            {isUnreachable ? '--' : value}
          </span>
          {detail ? <span className="text-xs text-muted-foreground">{detail}</span> : null}
        </div>

        {hideBar ? null : renderUtilizationBar(label, clampedRatio, widthPct, isUnreachable, isHot)}

        {isUnreachable ? (
          <p className="text-xs text-muted-foreground">{unreachableMessage}</p>
        ) : null}
        {!isUnreachable && caption ? renderCaptionLines(captionLines) : null}

        {expandable ? <div className="pt-1">{expandable}</div> : null}
      </CardContent>
    </Card>
  );
}
