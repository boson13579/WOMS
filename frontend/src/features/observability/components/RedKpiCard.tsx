/**
 * RedKpiCard — single KPI tile used by the RED metrics row.
 *
 * Presentational only: receives a label, big number, unit, optional
 * delta-vs-previous, optional subtitle (used by the SLO card to show
 * target + remaining budget), optional sparkline data, and optional
 * tone (controls border colour for error states and the delta colour).
 *
 * Reused for all four cards in the RED row including the SLO card —
 * keeps the visual rhythm consistent and avoids forking a near-identical
 * component just to change a subtitle.
 */
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';

import { Sparkline } from './Sparkline';

export type KpiTone = 'positive' | 'negative' | 'neutral' | 'warning' | 'critical';

interface RedKpiCardProps {
  label: string;
  /** Big number rendered with ``tabular-nums``. */
  value: string;
  /** Suffix appended to the value (e.g. ``"/s"`` or ``"%"``). */
  unit?: string | undefined;
  /** Inline delta string (e.g. ``"up 3% vs 5m"``); optional. */
  delta?: string | undefined;
  /** Tone influences both the delta colour and the border. */
  tone?: KpiTone | undefined;
  /** Optional second subtitle line (used by the SLO card). */
  subtitle?: string | undefined;
  /**
   * Optional muted line rendered below the subtitle. Used by the SLO card
   * to surface "data: last Xm" when the actual sample window is shorter
   * than the requested ``window_hours``. Always styled muted + xs.
   */
  footnote?: string | undefined;
  /** Ring-buffer values for the embedded sparkline; <3 entries renders nothing. */
  sparklineData?: number[] | undefined;
  /** ARIA label for the sparkline container. */
  sparklineLabel?: string | undefined;
}

const DELTA_TONE_CLASS: Record<KpiTone, string> = {
  positive: 'text-emerald-600 dark:text-emerald-400',
  negative: 'text-destructive',
  warning: 'text-amber-600 dark:text-amber-400',
  critical: 'text-destructive',
  neutral: 'text-muted-foreground',
};

const VALUE_TONE_CLASS: Record<KpiTone, string> = {
  positive: 'text-emerald-600 dark:text-emerald-400',
  negative: 'text-destructive',
  warning: 'text-amber-600 dark:text-amber-400',
  critical: 'text-destructive',
  neutral: '',
};

const CARD_TONE_CLASS: Partial<Record<KpiTone, string>> = {
  negative: 'border-destructive/40',
  critical: 'border-destructive/40',
};

export function RedKpiCard({
  label,
  value,
  unit,
  delta,
  tone = 'neutral',
  subtitle,
  footnote,
  sparklineData,
  sparklineLabel,
}: RedKpiCardProps): JSX.Element {
  return (
    <Card className={cn(CARD_TONE_CLASS[tone])}>
      <CardHeader className="pb-1">
        <CardTitle className="text-sm font-medium text-muted-foreground">{label}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex items-baseline gap-1">
          <span
            className={cn(
              'text-3xl font-semibold tabular-nums tracking-tight',
              VALUE_TONE_CLASS[tone],
            )}
          >
            {value}
          </span>
          {unit ? <span className="text-sm text-muted-foreground">{unit}</span> : null}
        </div>
        {delta !== undefined || subtitle !== undefined || footnote !== undefined ? (
          <div className="space-y-0.5">
            {delta ? <p className={cn('text-xs', DELTA_TONE_CLASS[tone])}>{delta}</p> : null}
            {subtitle ? <p className="text-xs text-muted-foreground">{subtitle}</p> : null}
            {footnote ? (
              <p className="text-xs text-muted-foreground" data-testid="red-kpi-footnote">
                {footnote}
              </p>
            ) : null}
          </div>
        ) : null}
        {sparklineData ? <Sparkline values={sparklineData} ariaLabel={sparklineLabel} /> : null}
      </CardContent>
    </Card>
  );
}
