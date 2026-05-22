/**
 * AuditTable — loading / empty / error / data states + expand toggle.
 */
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AuditEvent, AuditEventListResponse } from '../types';

import { AuditTable } from './AuditTable';

vi.mock('@/features/users/api/useUsernames', () => ({
  useUsernames: () => ({
    data: { '22222222-2222-2222-2222-222222222222': 'alice' },
  }),
}));

function makeEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id: '11111111-1111-1111-1111-111111111111',
    action: 'user.login_succeeded',
    user_id: '22222222-2222-2222-2222-222222222222',
    resource_id: '33333333-3333-3333-3333-333333333333',
    old_value: null,
    new_value: { ip: '127.0.0.1' },
    created_at: '2026-05-20T14:32:10Z',
    ...overrides,
  };
}

function makeList(items: AuditEvent[], total = items.length): AuditEventListResponse {
  return { items, total, page: 1, page_size: 20 };
}

function renderTable(overrides: Partial<Parameters<typeof AuditTable>[0]> = {}) {
  const props = {
    data: makeList([makeEvent()]),
    isLoading: false,
    isError: false,
    isFetching: false,
    page: 1,
    pageSize: 20 as const,
    onPageChange: vi.fn(),
    onPageSizeChange: vi.fn(),
    onRetry: vi.fn(),
    onClearFilters: vi.fn(),
    ...overrides,
  };
  return render(<AuditTable {...props} />);
}

describe('AuditTable', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders 5 skeleton rows while loading', () => {
    renderTable({ isLoading: true, data: undefined });
    expect(screen.getAllByTestId('audit-skeleton-row')).toHaveLength(5);
  });

  it('renders an alert + Retry button when isError', async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();
    renderTable({ isError: true, data: undefined, onRetry });
    expect(screen.getByRole('alert')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Retry/i }));
    expect(onRetry).toHaveBeenCalled();
  });

  it('renders an empty-state with Clear filters CTA when there are no events', async () => {
    const onClearFilters = vi.fn();
    const user = userEvent.setup();
    renderTable({ data: makeList([], 0), onClearFilters });
    expect(screen.getByText(/No events match your filters\./i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Clear filters/i }));
    expect(onClearFilters).toHaveBeenCalled();
  });

  it('renders event rows when data is present', () => {
    renderTable({ data: makeList([makeEvent({ action: 'order.created' })]) });
    expect(screen.getByTestId(/^audit-row-/)).toBeInTheDocument();
    expect(screen.getByTestId('action-chip')).toHaveTextContent('order.created');
  });

  it('toggles a row open and shows the JSON diff when clicked', async () => {
    const user = userEvent.setup();
    const event = makeEvent({
      action: 'order.updated',
      old_value: { quantity: 100 },
      new_value: { quantity: 200 },
    });
    renderTable({ data: makeList([event]) });

    const row = screen.getByTestId(`audit-row-${event.id}`);
    expect(screen.queryByTestId(`audit-row-detail-${event.id}`)).not.toBeInTheDocument();

    await user.click(row);

    expect(screen.getByTestId(`audit-row-detail-${event.id}`)).toBeInTheDocument();
    expect(screen.getByTestId('json-diff')).toBeInTheDocument();
  });

  it('resolves the actor name via useUsernames', () => {
    renderTable({ data: makeList([makeEvent()]) });
    expect(screen.getByText('alice')).toBeInTheDocument();
  });

  it('shows "Refreshing…" while isFetching is true', () => {
    renderTable({ isFetching: true });
    expect(screen.getByText(/Refreshing/i)).toBeInTheDocument();
  });
});
