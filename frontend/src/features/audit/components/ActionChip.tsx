/**
 * Coloured chip per action category.
 *
 * Maps the prefix before the first `.` of the dotted action name to a
 * Tailwind tone:
 *   - `user.*`     → sky
 *   - `order.*`    → emerald
 *   - `schedule.*` → violet
 *   - others       → muted (semantic token, works in both light and dark)
 *
 * Tested as a pure component so the palette mapping has a regression
 * guard — accidental tone swaps would otherwise show up only in design
 * review.
 */
import { cn } from '@/lib/utils';

interface ActionChipProps {
  action: string;
  className?: string;
}

function chipClass(action: string): string {
  if (action.startsWith('user.')) {
    return 'bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-300';
  }
  if (action.startsWith('order.')) {
    return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300';
  }
  if (action.startsWith('schedule.')) {
    return 'bg-violet-100 text-violet-700 dark:bg-violet-900/30 dark:text-violet-300';
  }
  return 'bg-muted text-muted-foreground';
}

export function ActionChip({ action, className }: ActionChipProps): JSX.Element {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 font-mono text-xs',
        chipClass(action),
        className,
      )}
      data-testid="action-chip"
      data-action={action}
    >
      {action}
    </span>
  );
}
