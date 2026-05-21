/**
 * WorkersDrilldown — per-worker disclosure for the USE Workers card.
 *
 * Renders a tiny shadcn-style chevron toggle followed by a list of
 * hostname / status / active_tasks rows. The component is responsible
 * for both the toggle UI and the conditional render of the list; the
 * parent decides whether to render this at all via the
 * ``workers.length > 1`` guard (single-worker drilldown adds no info).
 *
 * No new dependencies: ``Collapsible`` is not part of the project's
 * shadcn surface yet, so we build the inline disclosure described in
 * the plan body (Button + chevron + local useState).
 */
import { ChevronDown, ChevronUp } from 'lucide-react';
import { useState } from 'react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import type { WorkerBreakdown } from '../types';

interface WorkersDrilldownProps {
  workers: WorkerBreakdown[];
}

export function WorkersDrilldown({ workers }: WorkersDrilldownProps): JSX.Element {
  const [open, setOpen] = useState(false);

  return (
    <div className="space-y-2">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => {
          setOpen((prev) => !prev);
        }}
        aria-expanded={open}
        aria-controls="workers-drilldown-list"
        className="h-7 -ml-1 px-1.5 text-xs"
      >
        {open ? (
          <ChevronUp className="mr-1 h-3.5 w-3.5" aria-hidden />
        ) : (
          <ChevronDown className="mr-1 h-3.5 w-3.5" aria-hidden />
        )}
        {open ? 'Hide per-worker' : 'Show per-worker'}
      </Button>

      {open ? (
        <ul id="workers-drilldown-list" className="space-y-1">
          {workers.length === 0 ? (
            <li className="text-xs text-amber-600 dark:text-amber-400">
              No workers responded to inspect().
            </li>
          ) : (
            workers.map((w) => (
              <li
                key={w.hostname}
                className="flex items-center justify-between gap-2 rounded-sm px-1.5 py-1 text-xs"
              >
                <span className="truncate font-mono">{w.hostname}</span>
                <div className="flex shrink-0 items-center gap-2">
                  <span
                    className={cn(
                      'text-[10px] font-medium uppercase tracking-wider',
                      w.status === 'active'
                        ? 'text-emerald-600 dark:text-emerald-400'
                        : 'text-muted-foreground',
                    )}
                  >
                    {w.status}
                  </span>
                  <span className="tabular-nums text-muted-foreground">
                    {w.active_tasks === 0
                      ? '–'
                      : `${w.active_tasks} task${w.active_tasks === 1 ? '' : 's'}`}
                  </span>
                </div>
              </li>
            ))
          )}
        </ul>
      ) : null}
    </div>
  );
}
