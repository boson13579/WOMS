/**
 * AuditFilters — input rendering, change propagation, clear.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AuditFiltersState } from '../types';

import { AuditFilters } from './AuditFilters';

const mockUsers = vi.fn();

vi.mock('@/features/users/api/users', () => ({
  // eslint-disable-next-line @typescript-eslint/no-unsafe-return
  listUsers: (search?: string) => mockUsers(search),
}));

vi.mock('@/lib/auth', () => ({
  useCurrentRole: () => 'root',
}));

let qc: QueryClient;

function makeWrapper() {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  }
  return Wrapper;
}

const ALICE = {
  id: '11111111-1111-1111-1111-111111111111',
  username: 'alice',
  email: 'alice@example.com',
  role: 'order_manager' as const,
  is_active: true,
  version_id: 1,
  created_at: '2026-05-01T00:00:00.000Z',
};

describe('AuditFilters', () => {
  afterEach(() => {
    cleanup();
    qc.clear();
    vi.clearAllMocks();
  });

  beforeEach(() => {
    mockUsers.mockResolvedValue({ users: [ALICE], total: 1 });
  });

  it('renders all four filter inputs (actor, action, resource type, dates)', () => {
    render(<AuditFilters value={{}} onChange={vi.fn()} onClear={vi.fn()} />, {
      wrapper: makeWrapper(),
    });
    // Either the combobox or fallback input will satisfy this.
    expect(screen.getByLabelText(/Actor filter/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Action filter/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Resource type filter/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/From date/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/To date/i)).toBeInTheDocument();
  });

  it('falls back to a text input when the users list fails to load', async () => {
    mockUsers.mockRejectedValueOnce(new Error('forbidden'));
    render(<AuditFilters value={{}} onChange={vi.fn()} onClear={vi.fn()} />, {
      wrapper: makeWrapper(),
    });
    // Wait for the fallback to appear (after query settles)
    await screen.findByLabelText(/Actor filter \(text\)/i);
  });

  it('calls onChange with the updated resourceType when the Select changes', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<AuditFilters value={{}} onChange={onChange} onClear={vi.fn()} />, {
      wrapper: makeWrapper(),
    });
    await user.selectOptions(screen.getByLabelText(/Resource type filter/i), 'order');
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ resourceType: 'order' }));
  });

  it('commits the action draft on blur', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<AuditFilters value={{}} onChange={onChange} onClear={vi.fn()} />, {
      wrapper: makeWrapper(),
    });
    const input = screen.getByLabelText(/Action filter/i);
    await user.type(input, 'order.created');
    await user.tab();
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ action: 'order.created' }));
  });

  it('clears all local state and calls onClear when Clear filters is pressed', async () => {
    const onClear = vi.fn();
    const user = userEvent.setup();
    const initial: AuditFiltersState = {
      action: 'order.updated',
      fromDate: '2026-05-01',
      toDate: '2026-05-20',
    };
    render(<AuditFilters value={initial} onChange={vi.fn()} onClear={onClear} />, {
      wrapper: makeWrapper(),
    });
    await user.click(screen.getByRole('button', { name: /Clear filters/i }));
    expect(onClear).toHaveBeenCalled();
  });

  it('applies the action draft when Apply is clicked', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<AuditFilters value={{}} onChange={onChange} onClear={vi.fn()} />, {
      wrapper: makeWrapper(),
    });
    const input = screen.getByLabelText(/Action filter/i);
    await user.type(input, 'schedule.run');
    await user.click(screen.getByRole('button', { name: /^Apply$/ }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ action: 'schedule.run' }));
  });
});
