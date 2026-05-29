/**
 * TimeRangeSelector — segmented pill picker for the RED window.
 *
 * Renders four small outline buttons (60 / 300 / 900 / 3600 s) and lifts
 * the selection back to the parent via ``onChange``. Pure presentation +
 * a click handler; the parent owns the window state so the
 * ``useRedMetrics`` query key changes on click and React Query refetches
 * automatically.
 */
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { RED_WINDOW_OPTIONS, type RedWindowSeconds } from '../types';

interface TimeRangeSelectorProps {
  value: RedWindowSeconds;
  onChange: (next: RedWindowSeconds) => void;
}

// Display labels — keep short so the row fits on narrow viewports.
const LABELS: Record<RedWindowSeconds, string> = {
  60: '1m',
  300: '5m',
  900: '15m',
  3600: '1h',
};

export function TimeRangeSelector({
  value,
  onChange,
}: Readonly<TimeRangeSelectorProps>): JSX.Element {
  return (
    <fieldset className="inline-flex items-center gap-1 border-0 p-0 m-0">
      <legend className="sr-only">Select time range</legend>
      {RED_WINDOW_OPTIONS.map((opt) => {
        const isActive = opt === value;
        return (
          <Button
            key={opt}
            type="button"
            size="sm"
            variant={isActive ? 'secondary' : 'outline'}
            onClick={() => {
              onChange(opt);
            }}
            aria-pressed={isActive}
            className={cn('h-7 px-2.5 text-xs tabular-nums', isActive && 'font-semibold')}
          >
            {LABELS[opt]}
          </Button>
        );
      })}
    </fieldset>
  );
}
