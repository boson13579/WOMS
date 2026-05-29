/**
 * Side-by-side renderer for an audit event's `old_value` / `new_value`.
 *
 * Render rules (per plan):
 *   1. Both null → muted "(no field details)".
 *   2. Either side a flat object → 2-column layout. Left "Before"
 *      (rose tint), right "After" (emerald tint). Within each column,
 *      render `key: value` rows; values that are themselves objects/
 *      arrays serialise via `JSON.stringify(..., null, 2)`.
 *   3. Only one side present → present side full-width with a muted
 *      "—" for the missing side (creation / deletion case).
 *   4. Sensitive-looking keys (`password*`, `token*`, `secret*`) are
 *      masked client side as `••••` even though the backend redacts —
 *      defence in depth.
 */
import { cn } from '@/lib/utils';

interface JsonDiffViewProps {
  oldValue: Record<string, unknown> | null;
  newValue: Record<string, unknown> | null;
}

const SENSITIVE_KEY_REGEX = /(password|token|secret|api[_-]?key)/i;

function isSensitive(key: string): boolean {
  return SENSITIVE_KEY_REGEX.test(key);
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return '(unserialisable value)';
  }
}

interface JsonObjectPanelProps {
  label: string;
  data: Record<string, unknown> | null;
  tone: 'before' | 'after';
}

function JsonObjectPanel({ label, data, tone }: Readonly<JsonObjectPanelProps>): JSX.Element {
  const toneClass =
    tone === 'before'
      ? 'bg-rose-50 dark:bg-rose-950/30 border-rose-200/60 dark:border-rose-900/40'
      : 'bg-emerald-50 dark:bg-emerald-950/30 border-emerald-200/60 dark:border-emerald-900/40';

  if (data === null) {
    return (
      <div
        className={cn('rounded-md border p-3 text-xs', toneClass)}
        data-testid={`json-panel-${tone}`}
      >
        <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          {label}
        </p>
        <p className="italic text-muted-foreground">—</p>
      </div>
    );
  }

  const entries = Object.entries(data);

  return (
    <div
      className={cn('rounded-md border p-3 text-xs', toneClass)}
      data-testid={`json-panel-${tone}`}
    >
      <p className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        {label}
      </p>
      {entries.length === 0 ? (
        <p className="italic text-muted-foreground">(empty object)</p>
      ) : (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
          {entries.map(([key, value]) => {
            const display = isSensitive(key) ? '••••' : renderValue(value);
            const isMultiline = display.includes('\n');
            return (
              <div key={key} className="contents">
                <dt className="font-mono font-medium text-foreground/80">{key}:</dt>
                <dd className="font-mono break-words whitespace-pre-wrap text-foreground/90">
                  {isMultiline ? <pre className="m-0 whitespace-pre-wrap">{display}</pre> : display}
                </dd>
              </div>
            );
          })}
        </dl>
      )}
    </div>
  );
}

export function JsonDiffView({ oldValue, newValue }: Readonly<JsonDiffViewProps>): JSX.Element {
  if (oldValue === null && newValue === null) {
    return (
      <p className="italic text-muted-foreground" data-testid="json-diff-empty">
        (no field details)
      </p>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2" data-testid="json-diff">
      <JsonObjectPanel label="Before" data={oldValue} tone="before" />
      <JsonObjectPanel label="After" data={newValue} tone="after" />
    </div>
  );
}
