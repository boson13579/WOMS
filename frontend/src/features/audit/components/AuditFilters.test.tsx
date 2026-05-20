/**
 * AuditFilters — input rendering, change propagation, clear.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AuditFiltersState } from '../types';

import { AuditFilters } from './AuditFilters';

const mockUsers = vi.fn();
const mockUseAuditActions = vi.fn();

vi.mock('@/features/users/api/users', () => ({
  // eslint-disable-next-line @typescript-eslint/no-unsafe-return
  listUsers: (search?: string) => mockUsers(search),
}));

vi.mock('@/lib/auth', () => ({
  useCurrentRole: () => 'root',
}));

vi.mock('../api/useAuditActions', () => ({
  // eslint-disable-next-line @typescript-eslint/no-unsafe-return
  useAuditActions: () => mockUseAuditActions(),
}));

/** Default audit-actions query stub — success state with no rows. */
function setAuditActionsData(actions: string[]): void {
  mockUseAuditActions.mockReturnValue({
    data: { actions },
    isLoading: false,
    isError: false,
    isSuccess: true,
  });
}

function setAuditActionsLoading(): void {
  mockUseAuditActions.mockReturnValue({
    data: undefined,
    isLoading: true,
    isError: false,
    isSuccess: false,
  });
}

function setAuditActionsError(): void {
  mockUseAuditActions.mockReturnValue({
    data: undefined,
    isLoading: false,
    isError: true,
    isSuccess: false,
  });
}

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

const BOB = {
  id: '22222222-2222-2222-2222-222222222222',
  username: 'bob',
  email: 'bob@placeholder.internal',
  role: 'viewer' as const,
  is_active: true,
  version_id: 1,
  created_at: '2026-05-01T00:00:00.000Z',
};

const CAROL = {
  id: '33333333-3333-3333-3333-333333333333',
  username: 'carol',
  email: 'carol@placeholder.internal',
  role: 'scheduler' as const,
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
    // Default audit-actions stub: empty success. Each test that needs
    // populated suggestions calls ``setAuditActionsData(...)`` to override.
    setAuditActionsData([]);
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
    await user.click(input);
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

  describe('actor combobox', () => {
    beforeEach(() => {
      mockUsers.mockResolvedValue({ users: [ALICE, BOB, CAROL], total: 3 });
    });

    it('filters the dropdown by username substring', async () => {
      const user = userEvent.setup();
      render(<AuditFilters value={{}} onChange={vi.fn()} onClear={vi.fn()} />, {
        wrapper: makeWrapper(),
      });
      // Wait for users to load (combobox renders when userOptions > 0).
      const combobox = await screen.findByRole('combobox', { name: /Actor filter/i });
      await user.click(combobox);
      // All three rows visible on focus.
      expect(screen.getByRole('option', { name: /alice/i })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: /bob/i })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: /carol/i })).toBeInTheDocument();

      await user.type(combobox, 'alic');
      // Only alice remains.
      expect(screen.getByRole('option', { name: /alice/i })).toBeInTheDocument();
      expect(screen.queryByRole('option', { name: /^bob/i })).not.toBeInTheDocument();
      expect(screen.queryByRole('option', { name: /^carol/i })).not.toBeInTheDocument();
    });

    it('filters the dropdown by email substring', async () => {
      const user = userEvent.setup();
      render(<AuditFilters value={{}} onChange={vi.fn()} onClear={vi.fn()} />, {
        wrapper: makeWrapper(),
      });
      const combobox = await screen.findByRole('combobox', { name: /Actor filter/i });
      await user.click(combobox);

      // @placeholder matches Bob and Carol (both legacy backfill).
      await user.type(combobox, '@placeholder');
      expect(screen.getByRole('option', { name: /bob/i })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: /carol/i })).toBeInTheDocument();
      // Alice has @example.com, should be filtered out.
      expect(screen.queryByRole('option', { name: /alice/i })).not.toBeInTheDocument();

      // Narrow to just carol's email.
      await user.clear(combobox);
      await user.type(combobox, 'carol@');
      expect(screen.getByRole('option', { name: /carol/i })).toBeInTheDocument();
      expect(screen.queryByRole('option', { name: /^bob/i })).not.toBeInTheDocument();
    });

    it('selecting a user sets actor_id and closes the dropdown', async () => {
      const onChange = vi.fn();
      const user = userEvent.setup();
      render(<AuditFilters value={{}} onChange={onChange} onClear={vi.fn()} />, {
        wrapper: makeWrapper(),
      });
      const combobox = await screen.findByRole('combobox', { name: /Actor filter/i });
      await user.click(combobox);
      const bobOption = screen.getByRole('option', { name: /bob/i });

      // Use fireEvent.mouseDown because the row commits on mousedown
      // (so the input's blur can't race the click).
      fireEvent.mouseDown(bobOption);

      expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ actorId: BOB.id }));
      // Listbox is gone.
      await waitFor(() => {
        expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
      });
    });

    it('clear button deselects the actor', async () => {
      const onChange = vi.fn();
      const user = userEvent.setup();
      render(<AuditFilters value={{ actorId: ALICE.id }} onChange={onChange} onClear={vi.fn()} />, {
        wrapper: makeWrapper(),
      });
      // Wait for the combobox to appear with the selected user's name.
      const combobox = await screen.findByRole('combobox', { name: /Actor filter/i });
      await waitFor(() => {
        expect((combobox as HTMLInputElement).value).toBe('alice');
      });

      const clearBtn = screen.getByRole('button', { name: /Clear actor filter/i });
      await user.click(clearBtn);
      expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ actorId: undefined }));
    });

    it('closes the dropdown on outside click', async () => {
      const user = userEvent.setup();
      render(
        <div>
          <AuditFilters value={{}} onChange={vi.fn()} onClear={vi.fn()} />
          <button type="button" data-testid="outside">
            outside
          </button>
        </div>,
        { wrapper: makeWrapper() },
      );
      const combobox = await screen.findByRole('combobox', { name: /Actor filter/i });
      await user.click(combobox);
      expect(screen.getByRole('listbox')).toBeInTheDocument();

      // Outside click — simulate a mousedown on the body element.
      fireEvent.mouseDown(screen.getByTestId('outside'));
      await waitFor(() => {
        expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
      });
    });

    it('falls back to the text input when the users list fails', async () => {
      mockUsers.mockRejectedValueOnce(new Error('forbidden'));
      render(<AuditFilters value={{}} onChange={vi.fn()} onClear={vi.fn()} />, {
        wrapper: makeWrapper(),
      });
      // The fallback Input is labelled "Actor filter (text)".
      await screen.findByLabelText(/Actor filter \(text\)/i);
      expect(screen.queryByRole('combobox', { name: /Actor filter/i })).not.toBeInTheDocument();
    });
  });

  describe('action combobox', () => {
    it('filters the dropdown by substring (e.g. "user" narrows to user.* actions)', async () => {
      setAuditActionsData(['order.created', 'order.deleted', 'user.login_succeeded']);
      const user = userEvent.setup();
      render(<AuditFilters value={{}} onChange={vi.fn()} onClear={vi.fn()} />, {
        wrapper: makeWrapper(),
      });
      const combobox = screen.getByRole('combobox', { name: /Action filter/i });
      await user.click(combobox);

      // All three rows visible on focus.
      expect(screen.getByRole('option', { name: 'order.created' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'order.deleted' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'user.login_succeeded' })).toBeInTheDocument();

      await user.type(combobox, 'user');
      expect(screen.getByRole('option', { name: 'user.login_succeeded' })).toBeInTheDocument();
      expect(screen.queryByRole('option', { name: 'order.created' })).not.toBeInTheDocument();
      expect(screen.queryByRole('option', { name: 'order.deleted' })).not.toBeInTheDocument();
    });

    it('commits free-text that is not in the known list (blur path)', async () => {
      // No 'custom.action' in the suggestion list.
      setAuditActionsData(['order.created', 'user.login_succeeded']);
      const onChange = vi.fn();
      const user = userEvent.setup();
      render(<AuditFilters value={{}} onChange={onChange} onClear={vi.fn()} />, {
        wrapper: makeWrapper(),
      });
      const combobox = screen.getByRole('combobox', { name: /Action filter/i });
      await user.click(combobox);
      await user.type(combobox, 'custom.action');
      await user.tab();
      expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ action: 'custom.action' }));
    });

    it('selecting a known action via mousedown commits that exact string', async () => {
      setAuditActionsData(['order.created', 'user.login_succeeded']);
      const onChange = vi.fn();
      const user = userEvent.setup();
      render(<AuditFilters value={{}} onChange={onChange} onClear={vi.fn()} />, {
        wrapper: makeWrapper(),
      });
      const combobox = screen.getByRole('combobox', { name: /Action filter/i });
      await user.click(combobox);
      const option = screen.getByRole('option', { name: 'user.login_succeeded' });
      // Row commits on mousedown so the input's blur can't race the click.
      fireEvent.mouseDown(option);
      expect(onChange).toHaveBeenCalledWith(
        expect.objectContaining({ action: 'user.login_succeeded' }),
      );
    });

    it('closes the dropdown on outside click', async () => {
      setAuditActionsData(['order.created']);
      const user = userEvent.setup();
      render(
        <div>
          <AuditFilters value={{}} onChange={vi.fn()} onClear={vi.fn()} />
          <button type="button" data-testid="outside-action">
            outside
          </button>
        </div>,
        { wrapper: makeWrapper() },
      );
      const combobox = screen.getByRole('combobox', { name: /Action filter/i });
      await user.click(combobox);
      expect(screen.getByRole('listbox', { name: /Action suggestions/i })).toBeInTheDocument();

      fireEvent.mouseDown(screen.getByTestId('outside-action'));
      await waitFor(() => {
        expect(
          screen.queryByRole('listbox', { name: /Action suggestions/i }),
        ).not.toBeInTheDocument();
      });
    });

    it('shows a loading placeholder while the actions query is pending', async () => {
      setAuditActionsLoading();
      const user = userEvent.setup();
      render(<AuditFilters value={{}} onChange={vi.fn()} onClear={vi.fn()} />, {
        wrapper: makeWrapper(),
      });
      const combobox = screen.getByRole('combobox', { name: /Action filter/i });
      await user.click(combobox);
      expect(screen.getByText(/Loading actions/i)).toBeInTheDocument();
    });

    it('shows the freeform-friendly empty state when the query errors out', async () => {
      setAuditActionsError();
      const user = userEvent.setup();
      render(<AuditFilters value={{}} onChange={vi.fn()} onClear={vi.fn()} />, {
        wrapper: makeWrapper(),
      });
      const combobox = screen.getByRole('combobox', { name: /Action filter/i });
      await user.click(combobox);
      expect(screen.getByText(/no actions available/i)).toBeInTheDocument();
    });
  });
});
