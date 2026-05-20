/**
 * Single audit-event row.
 *
 * - Time column: `formatInTimeZone` is not pulled in (no extra dep
 *   per plan); we hand-roll a UTC formatter that produces
 *   `YYYY-MM-DD HH:mm:ss UTC` and suffixes `UTC` so it's obvious the
 *   stamp is server-side.
 * - Actor column: resolved via `useUsernames` upstream — the parent
 *   passes in the resolved name (or `null` for "(system)").
 * - Resource column: `type/<first-8-of-uuid>` with full UUID in
 *   the `title` attribute for hover copy.
 * - Detail row: a second `<TableRow>` spanning all columns; rendered
 *   only when expanded so collapsed rows don't pay the cost of
 *   instantiating `JsonDiffView`.
 */
import { ChevronDown, ChevronRight } from 'lucide-react';

import { TableCell, TableRow } from '@/components/ui/table';
import { cn } from '@/lib/utils';

import type { AuditEvent } from '../types';

import { ActionChip } from './ActionChip';
import { JsonDiffView } from './JsonDiffView';

interface AuditRowProps {
  event: AuditEvent;
  /** Resolved actor display name. `null` ⇒ render "(system)" italic. */
  actorName: string | null;
  expanded: boolean;
  onToggle: () => void;
  /** Number of head columns the detail row should span. */
  colSpan: number;
}

/**
 * Format `created_at` as `YYYY-MM-DD HH:mm:ss UTC`.
 * Falls back to the raw string if parsing fails (defensive — backend
 * always emits ISO-8601, but Zod parses strings unchecked).
 */
function formatUtc(created: string): string {
  const d = new Date(created);
  if (Number.isNaN(d.getTime())) return `${created} UTC`;
  const yy = d.getUTCFullYear();
  const mo = String(d.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  const ss = String(d.getUTCSeconds()).padStart(2, '0');
  return `${yy}-${mo}-${dd} ${hh}:${mm}:${ss} UTC`;
}

function inferResourceType(action: string): string {
  const prefix = action.split('.', 1)[0];
  return prefix || 'event';
}

function shortId(uuid: string): string {
  return uuid.slice(0, 8);
}

export function AuditRow({
  event,
  actorName,
  expanded,
  onToggle,
  colSpan,
}: AuditRowProps): JSX.Element {
  const resourceType = inferResourceType(event.action);

  return (
    <>
      <TableRow
        className="cursor-pointer hover:bg-muted/40"
        onClick={onToggle}
        data-testid={`audit-row-${event.id}`}
        aria-expanded={expanded}
      >
        <TableCell className="w-6 text-muted-foreground">
          {expanded ? (
            <ChevronDown className="h-4 w-4" aria-hidden="true" />
          ) : (
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          )}
        </TableCell>
        <TableCell
          className="whitespace-nowrap font-mono text-xs text-muted-foreground"
          title={event.created_at}
        >
          {formatUtc(event.created_at)}
        </TableCell>
        <TableCell
          className={cn(
            'font-medium',
            event.user_id === null ? 'italic text-muted-foreground' : '',
          )}
          title={event.user_id ?? '(system)'}
        >
          {event.user_id === null ? '(system)' : (actorName ?? '—')}
        </TableCell>
        <TableCell>
          <ActionChip action={event.action} />
        </TableCell>
        <TableCell
          className="font-mono text-xs text-muted-foreground"
          title={`${resourceType}/${event.resource_id}`}
        >
          {resourceType}/{shortId(event.resource_id)}…
        </TableCell>
      </TableRow>

      {expanded ? (
        <TableRow
          className="bg-muted/20 hover:bg-muted/20"
          data-testid={`audit-row-detail-${event.id}`}
        >
          <TableCell colSpan={colSpan} className="px-6 py-4">
            <div className="space-y-3">
              <div className="grid grid-cols-1 gap-2 text-xs text-muted-foreground md:grid-cols-3">
                <div>
                  <span className="font-semibold">Event ID:</span>{' '}
                  <span className="font-mono">{event.id}</span>
                </div>
                <div>
                  <span className="font-semibold">Resource ID:</span>{' '}
                  <span className="font-mono">{event.resource_id}</span>
                </div>
                <div>
                  <span className="font-semibold">Actor ID:</span>{' '}
                  <span className="font-mono">{event.user_id ?? '(system)'}</span>
                </div>
              </div>
              <JsonDiffView oldValue={event.old_value} newValue={event.new_value} />
            </div>
          </TableCell>
        </TableRow>
      ) : null}
    </>
  );
}
