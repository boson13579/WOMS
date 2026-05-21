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
 * free-text input so the page still operates. The combobox itself
 * is a small typeahead (`ActorCombobox` below) — a `<Select>` doesn't
 * scale once production has dozens of users to scroll past.
 *
 * Action combobox: same typeahead pattern as the actor picker, but
 * backed by ``useAuditActions`` (which hits ``GET /audit/actions``
 * for the live distinct action list). Free-text is accepted so an
 * admin can query an action that hasn't been emitted yet without
 * the dropdown blocking submission — useful while a feature ships.
 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useId, useMemo, useRef, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { listUsers } from '@/features/users/api/users';
import { type UserResponse } from '@/features/users/types/user';
import { useCurrentRole } from '@/lib/auth';

import { useAuditActions } from '../api/useAuditActions';
import { auditResourceTypeSchema, type AuditFiltersState, type AuditResourceType } from '../types';

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

/** Cap visible matches — 1000 rows in a popover scroll is awful. */
const MAX_VISIBLE_MATCHES = 10;

function clean<T extends string | undefined>(v: T): T | undefined {
  if (v === undefined) return undefined;
  const trimmed = v.trim();
  return (trimmed === '' ? undefined : trimmed) as T;
}

/**
 * Typeahead-style combobox for picking an audit actor.
 *
 * `<Select>` doesn't scale once production has many users — scrolling
 * a hundred-entry dropdown to find one person is unusable. This
 * component renders an `<Input>` that shows the selected user's
 * username and reveals a small filterable list on focus.
 *
 * Matching is a case-insensitive substring against `username` OR
 * `email`. Email of `*@placeholder.internal` (legacy migration
 * backfill) is still searchable so an admin can find old accounts.
 *
 * Click-outside follows the same pattern as `MobileNav.tsx`: a ref on
 * the wrapper plus a `mousedown` listener on `document`.
 */
function ActorCombobox({
  value,
  onChange,
  users,
}: {
  value: string | undefined;
  onChange: (id: string | undefined) => void;
  users: UserResponse[];
}): JSX.Element {
  const selectedUser = useMemo(
    () => (value ? users.find((u) => u.id === value) : undefined),
    [users, value],
  );

  // `inputValue` is the display string in the textbox. It tracks the
  // selected user's username when one is set, but follows the user's
  // raw keystrokes while typing — so `inputValue` is the source of
  // truth for the filter query.
  const [inputValue, setInputValue] = useState<string>(selectedUser?.username ?? '');
  const [isOpen, setIsOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();

  // Keep the input in sync when `value` changes externally (URL
  // hydration, Clear filters, etc.). Without this the input would
  // show a stale username after Back/Forward navigation.
  useEffect(() => {
    setInputValue(selectedUser?.username ?? '');
  }, [selectedUser]);

  // Click-outside closes the popover. Same recipe as MobileNav: a ref
  // on the wrapper, a `mousedown` listener on document. We use
  // `mousedown` rather than `click` so the dropdown closes before a
  // focus event on another widget fires (otherwise the next focus
  // would briefly re-open it).
  useEffect(() => {
    if (!isOpen) return undefined;
    function onMouseDown(e: MouseEvent): void {
      const wrapper = wrapperRef.current;
      if (!wrapper) return;
      if (e.target instanceof Node && wrapper.contains(e.target)) return;
      setIsOpen(false);
    }
    document.addEventListener('mousedown', onMouseDown);
    return () => {
      document.removeEventListener('mousedown', onMouseDown);
    };
  }, [isOpen]);

  // Filter against username OR email, case-insensitively. Empty query
  // shows the first MAX_VISIBLE_MATCHES users — gives the admin a
  // useful starting set without flooding the popover.
  const matches = useMemo(() => {
    const q = inputValue.trim().toLowerCase();
    const filtered = q
      ? users.filter(
          (u) =>
            u.username.toLowerCase().includes(q) || (u.email?.toLowerCase().includes(q) ?? false),
        )
      : users;
    return filtered.slice(0, MAX_VISIBLE_MATCHES);
  }, [inputValue, users]);

  // Keep highlight in range after the filter shrinks below it.
  useEffect(() => {
    if (highlight >= matches.length) {
      setHighlight(Math.max(0, matches.length - 1));
    }
  }, [matches.length, highlight]);

  function selectUser(user: UserResponse): void {
    onChange(user.id);
    setInputValue(user.username);
    setIsOpen(false);
  }

  function handleClear(): void {
    onChange(undefined);
    setInputValue('');
    setIsOpen(false);
  }

  return (
    <div ref={wrapperRef} className="relative">
      <div className="relative">
        <Input
          id="audit-filter-actor"
          type="text"
          placeholder="Search by name or email…"
          value={inputValue}
          onChange={(e) => {
            setInputValue(e.target.value);
            setIsOpen(true);
            setHighlight(0);
          }}
          onFocus={() => {
            setIsOpen(true);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              setIsOpen(false);
              return;
            }
            if (e.key === 'ArrowDown') {
              e.preventDefault();
              if (!isOpen) setIsOpen(true);
              setHighlight((h) => Math.min(matches.length - 1, h + 1));
              return;
            }
            if (e.key === 'ArrowUp') {
              e.preventDefault();
              setHighlight((h) => Math.max(0, h - 1));
              return;
            }
            if (e.key === 'Enter' && isOpen && matches.length > 0) {
              const target = matches[highlight];
              e.preventDefault();
              selectUser(target);
            }
          }}
          autoComplete="off"
          role="combobox"
          aria-expanded={isOpen}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-label="Actor filter"
          className={value ? 'pr-9' : undefined}
        />
        {value ? (
          <button
            type="button"
            onClick={handleClear}
            aria-label="Clear actor filter"
            className="absolute inset-y-0 right-0 flex items-center px-2 text-muted-foreground hover:text-foreground"
          >
            <span aria-hidden>✕</span>
          </button>
        ) : null}
      </div>

      {isOpen ? (
        <ul
          id={listboxId}
          role="listbox"
          aria-label="Actor suggestions"
          className="absolute left-0 right-0 top-full z-50 mt-1 max-h-72 overflow-auto rounded-md border border-border bg-popover py-1 text-popover-foreground shadow-md"
        >
          {matches.length === 0 ? (
            <li className="px-3 py-2 text-sm text-muted-foreground">No matches</li>
          ) : (
            matches.map((user, idx) => {
              const isHighlighted = idx === highlight;
              const isSelected = user.id === value;
              return (
                <li
                  key={user.id}
                  role="option"
                  aria-selected={isSelected}
                  data-testid={`actor-option-${user.username}`}
                  onMouseDown={(e) => {
                    // `mousedown` rather than `click`: blur on the
                    // input would otherwise close the popover before
                    // the click reached this row.
                    e.preventDefault();
                    selectUser(user);
                  }}
                  onMouseEnter={() => {
                    setHighlight(idx);
                  }}
                  className={`cursor-pointer px-3 py-1.5 text-sm ${
                    isHighlighted ? 'bg-accent text-accent-foreground' : ''
                  }`}
                >
                  <div className="font-semibold">{user.username}</div>
                  {user.email ? (
                    <div className="text-xs text-muted-foreground">{user.email}</div>
                  ) : null}
                </li>
              );
            })
          )}
        </ul>
      ) : null}
    </div>
  );
}

/**
 * Typeahead-style combobox for picking an audit action.
 *
 * Mirrors the `ActorCombobox` recipe (ref + mousedown click-outside,
 * `<Input>` shows the committed value, `<ul role="listbox">` reveals
 * a filtered list on focus). The differences from the actor picker:
 *
 *   - **Free-text accepted**: blur / Enter / outside-click with a
 *     non-empty draft commits that string, even if it doesn't match
 *     any known action. This is intentional — an admin may want to
 *     query for an action that hasn't been emitted yet (mid-feature
 *     rollout) without the combobox blocking submission.
 *   - **Loading / error states**: the actions list is fetched async;
 *     while pending we show a "Loading actions…" placeholder, and
 *     on error we surface "(no actions available — type freely)"
 *     so the typing path still works.
 *   - **No clear button**: the empty value already means "no filter",
 *     so the admin just clears the text and tabs out.
 *
 * `value` is the *committed* string the parent owns. `inputValue` is
 * the in-flight draft inside this component — it diverges while the
 * admin types and re-syncs to `value` whenever the parent updates it
 * (e.g. URL hydration, Clear filters).
 */
function ActionCombobox({
  value,
  onChange,
  actions,
  isLoading,
  isError,
}: {
  value: string | undefined;
  onChange: (action: string | undefined) => void;
  actions: string[];
  isLoading: boolean;
  isError: boolean;
}): JSX.Element {
  const [inputValue, setInputValue] = useState<string>(value ?? '');
  const [isOpen, setIsOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();

  // Re-sync from external `value` changes (URL hydration, Clear).
  useEffect(() => {
    setInputValue(value ?? '');
  }, [value]);

  // Click-outside closes the popover AND commits the in-flight draft
  // (same convention as blur — outside-click is morally a blur).
  // Using `mousedown` rather than `click` so the popover closes
  // before any focus event on another widget fires.
  useEffect(() => {
    if (!isOpen) return undefined;
    function onMouseDown(e: MouseEvent): void {
      const wrapper = wrapperRef.current;
      if (!wrapper) return;
      if (e.target instanceof Node && wrapper.contains(e.target)) return;
      setIsOpen(false);
    }
    document.addEventListener('mousedown', onMouseDown);
    return () => {
      document.removeEventListener('mousedown', onMouseDown);
    };
  }, [isOpen]);

  // Case-insensitive substring filter; empty query shows everything
  // (capped) so the admin gets a useful starting set on focus.
  const matches = useMemo(() => {
    const q = inputValue.trim().toLowerCase();
    const filtered = q ? actions.filter((a) => a.toLowerCase().includes(q)) : actions;
    return filtered.slice(0, MAX_VISIBLE_MATCHES);
  }, [actions, inputValue]);

  useEffect(() => {
    if (highlight >= matches.length) {
      setHighlight(Math.max(0, matches.length - 1));
    }
  }, [matches.length, highlight]);

  function commit(raw: string): void {
    const trimmed = raw.trim();
    onChange(trimmed === '' ? undefined : trimmed);
    setIsOpen(false);
  }

  function selectAction(action: string): void {
    setInputValue(action);
    commit(action);
  }

  return (
    <div ref={wrapperRef} className="relative">
      <Input
        id="audit-filter-action"
        type="text"
        placeholder="user.login_succeeded"
        value={inputValue}
        onChange={(e) => {
          setInputValue(e.target.value);
          setIsOpen(true);
          setHighlight(0);
        }}
        onFocus={() => {
          setIsOpen(true);
        }}
        onBlur={() => {
          // Free-text commit: whatever is in the input wins. Outside-
          // click goes through the document-level mousedown listener,
          // which sets `isOpen=false` but doesn't commit; this blur
          // handler is the one that finalises the draft.
          commit(inputValue);
        }}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            setIsOpen(false);
            return;
          }
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (!isOpen) setIsOpen(true);
            setHighlight((h) => Math.min(matches.length - 1, h + 1));
            return;
          }
          if (e.key === 'ArrowUp') {
            e.preventDefault();
            setHighlight((h) => Math.max(0, h - 1));
            return;
          }
          if (e.key === 'Enter') {
            e.preventDefault();
            // If a row is highlighted and visible, prefer it; otherwise
            // commit the free-text draft.
            if (isOpen && matches.length > 0) {
              selectAction(matches[highlight]);
            } else {
              commit(inputValue);
            }
          }
        }}
        autoComplete="off"
        role="combobox"
        aria-expanded={isOpen}
        aria-controls={listboxId}
        aria-autocomplete="list"
        aria-label="Action filter"
      />

      {isOpen ? (
        <ul
          id={listboxId}
          role="listbox"
          aria-label="Action suggestions"
          className="absolute left-0 right-0 top-full z-50 mt-1 max-h-72 overflow-auto rounded-md border border-border bg-popover py-1 text-popover-foreground shadow-md"
        >
          {renderActionListContent({
            isLoading,
            isError,
            matches,
            highlight,
            value,
            onPick: selectAction,
            onHover: setHighlight,
          })}
        </ul>
      ) : null}
    </div>
  );
}

/** Listbox body — split into a helper to keep the JSX free of nested ternaries. */
function renderActionListContent({
  isLoading,
  isError,
  matches,
  highlight,
  value,
  onPick,
  onHover,
}: {
  isLoading: boolean;
  isError: boolean;
  matches: string[];
  highlight: number;
  value: string | undefined;
  onPick: (action: string) => void;
  onHover: (idx: number) => void;
}): JSX.Element | JSX.Element[] {
  if (isLoading) {
    return <li className="px-3 py-2 text-sm text-muted-foreground">Loading actions…</li>;
  }
  if (isError) {
    return (
      <li className="px-3 py-2 text-sm text-muted-foreground">
        (no actions available — type freely)
      </li>
    );
  }
  if (matches.length === 0) {
    return (
      <li className="px-3 py-2 text-sm text-muted-foreground">
        No matches — press Enter to use this value
      </li>
    );
  }
  return matches.map((action, idx) => {
    const isHighlighted = idx === highlight;
    const isSelected = action === value;
    return (
      <li
        key={action}
        role="option"
        aria-selected={isSelected}
        data-testid={`action-option-${action}`}
        onMouseDown={(e) => {
          // `mousedown` so the input's blur doesn't race the click.
          // We call onPick directly rather than letting blur handle
          // it because the row is the explicit intent — preserve the
          // exact string.
          e.preventDefault();
          onPick(action);
        }}
        onMouseEnter={() => {
          onHover(idx);
        }}
        className={`cursor-pointer px-3 py-1.5 text-sm ${
          isHighlighted ? 'bg-accent text-accent-foreground' : ''
        }`}
      >
        {action}
      </li>
    );
  });
}

export function AuditFilters({ value, onChange, onClear }: AuditFiltersProps): JSX.Element {
  const role = useCurrentRole();

  const usersQuery = useQuery({
    queryKey: ['admin-users', ''],
    queryFn: () => listUsers(''),
    enabled: role === 'root',
    staleTime: 60_000,
  });

  const actionsQuery = useAuditActions();

  // Local string state for date inputs that apply on blur / Enter.
  // (The action input is owned by ``ActionCombobox`` and manages its
  // own in-flight draft + commit semantics.)
  const [fromDraft, setFromDraft] = useState(value.fromDate ?? '');
  const [toDraft, setToDraft] = useState(value.toDate ?? '');

  // Hydrate local drafts when URL params change (e.g. Back/Forward,
  // Clear filters). Keeping these in sync avoids a stale input.
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
  const actionOptions = actionsQuery.data?.actions ?? [];

  const commitText = (): void => {
    // Action commits via the combobox's own blur / select handlers;
    // here we only flush the date drafts that the form layer still owns.
    onChange({
      ...value,
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
              <ActorCombobox
                value={value.actorId}
                onChange={(id) => {
                  onChange({ ...value, actorId: id });
                }}
                users={userOptions}
              />
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
            <ActionCombobox
              value={value.action}
              onChange={(action) => {
                onChange({ ...value, action });
              }}
              actions={actionOptions}
              isLoading={actionsQuery.isLoading}
              isError={actionsQuery.isError}
            />
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
                // ActionCombobox re-syncs from `value.action` becoming
                // undefined (via the parent's onClear), so we don't
                // need to touch its internal draft from here.
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
