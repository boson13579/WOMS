/**
 * Audit-filter bar — actor / action / resource type / from / to.
 *
 * Hybrid apply behaviour (per plan):
 *   - Actor and resource-type select apply immediately on change.
 *   - Action and date inputs apply on blur OR on Enter OR on explicit
 *     [Apply] click. Local state holds the in-flight string; the
 *     parent only sees committed values via `onChange`.
 *   - [Clear filters] resets every field and bubbles a full empty
 *     filters object so the parent can drop the URL query string.
 *
 * Actor combobox: pulls the list once via `listUsers('')`. If the
 * fetch fails (non-root, network, etc.), the combobox degrades to a
 * free-text input so the page still operates.
 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useId, useMemo, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { listUsers } from '@/features/users/api/users';
import { useCurrentRole } from '@/lib/auth';

import {
  auditResourceTypeSchema,
  KNOWN_AUDIT_ACTIONS,
  type AuditFiltersState,
  type AuditResourceType,
} from '../types';

interface AuditFiltersProps {
  value: AuditFiltersState;
  onChange: (next: AuditFiltersState) => void;
  onClear: () => void;
}

const RESOURCE_OPTIONS: { value: '' | AuditResourceType; label: string }[] = [
  { value: '', label: '(any)' },
  { value: 'user', label: 'user' },
  { value: 'order', label: 'order' },
  { value: 'schedule', label: 'schedule' },
];

function clean<T extends string | undefined>(v: T): T | undefined {
  if (v === undefined) return undefined;
  const trimmed = v.trim();
  return (trimmed === '' ? undefined : trimmed) as T;
}

export function AuditFilters({ value, onChange, onClear }: AuditFiltersProps): JSX.Element {
  const role = useCurrentRole();
  const dataListId = useId();

  const usersQuery = useQuery({
    queryKey: ['admin-users', ''],
    queryFn: () => listUsers(''),
    enabled: role === 'root',
    staleTime: 60_000,
  });

  // Local string state for inputs that apply on blur / Enter.
  const [actionDraft, setActionDraft] = useState(value.action ?? '');
  const [fromDraft, setFromDraft] = useState(value.fromDate ?? '');
  const [toDraft, setToDraft] = useState(value.toDate ?? '');

  // Hydrate local drafts when URL params change (e.g. Back/Forward,
  // Clear filters). Keeping these in sync avoids a stale input.
  useEffect(() => {
    setActionDraft(value.action ?? '');
  }, [value.action]);
  useEffect(() => {
    setFromDraft(value.fromDate ?? '');
  }, [value.fromDate]);
  useEffect(() => {
    setToDraft(value.toDate ?? '');
  }, [value.toDate]);

  const userOptions = useMemo(
    () => usersQuery.data?.users.filter((u) => u.is_active) ?? [],
    [usersQuery.data],
  );
  const userListFailed = usersQuery.isError;

  const commitText = (): void => {
    onChange({
      ...value,
      action: clean(actionDraft),
      fromDate: clean(fromDraft),
      toDate: clean(toDraft),
    });
  };

  const handleApply = (event: React.FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    commitText();
  };

  return (
    <Card>
      <CardContent className="p-4">
        <form
          onSubmit={handleApply}
          className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4"
          data-testid="audit-filters-form"
        >
          {/* Actor */}
          <div className="space-y-1.5">
            <Label htmlFor="audit-filter-actor">Actor</Label>
            {!userListFailed && userOptions.length > 0 ? (
              <Select
                id="audit-filter-actor"
                value={value.actorId ?? ''}
                onChange={(event) => {
                  onChange({ ...value, actorId: clean(event.target.value) });
                }}
                aria-label="Actor filter"
              >
                <option value="">(any)</option>
                {userOptions.map((user) => (
                  <option key={user.id} value={user.id}>
                    {user.username}
                    {user.email ? ` (${user.email})` : ''}
                  </option>
                ))}
              </Select>
            ) : (
              <Input
                id="audit-filter-actor"
                placeholder="actor UUID"
                value={value.actorId ?? ''}
                onChange={(event) => {
                  onChange({ ...value, actorId: clean(event.target.value) });
                }}
                aria-label="Actor filter (text)"
              />
            )}
          </div>

          {/* Action */}
          <div className="space-y-1.5">
            <Label htmlFor="audit-filter-action">Action</Label>
            <Input
              id="audit-filter-action"
              list={dataListId}
              placeholder="user.login_succeeded"
              value={actionDraft}
              onChange={(event) => {
                setActionDraft(event.target.value);
              }}
              onBlur={() => {
                onChange({ ...value, action: clean(actionDraft) });
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  onChange({ ...value, action: clean(actionDraft) });
                }
              }}
              aria-label="Action filter"
            />
            <datalist id={dataListId}>
              {KNOWN_AUDIT_ACTIONS.map((action) => (
                <option key={action} value={action} />
              ))}
            </datalist>
          </div>

          {/* Resource type */}
          <div className="space-y-1.5">
            <Label htmlFor="audit-filter-resource">Resource type</Label>
            <Select
              id="audit-filter-resource"
              value={value.resourceType ?? ''}
              onChange={(event) => {
                const raw = event.target.value;
                if (raw === '') {
                  onChange({ ...value, resourceType: undefined });
                  return;
                }
                const parsed = auditResourceTypeSchema.safeParse(raw);
                if (parsed.success) {
                  onChange({ ...value, resourceType: parsed.data });
                }
              }}
              aria-label="Resource type filter"
            >
              {RESOURCE_OPTIONS.map((opt) => (
                <option key={opt.value || 'any'} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </Select>
          </div>

          {/* From / To */}
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1.5">
              <Label htmlFor="audit-filter-from">From</Label>
              <Input
                id="audit-filter-from"
                type="date"
                value={fromDraft}
                onChange={(event) => {
                  setFromDraft(event.target.value);
                }}
                onBlur={() => {
                  onChange({ ...value, fromDate: clean(fromDraft) });
                }}
                aria-label="From date"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="audit-filter-to">To</Label>
              <Input
                id="audit-filter-to"
                type="date"
                value={toDraft}
                onChange={(event) => {
                  setToDraft(event.target.value);
                }}
                onBlur={() => {
                  onChange({ ...value, toDate: clean(toDraft) });
                }}
                aria-label="To date"
              />
            </div>
          </div>

          {/* Actions row */}
          <div className="flex items-end justify-end gap-2 md:col-span-2 lg:col-span-4">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                setActionDraft('');
                setFromDraft('');
                setToDraft('');
                onClear();
              }}
            >
              Clear filters
            </Button>
            <Button type="submit" size="sm">
              Apply
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
