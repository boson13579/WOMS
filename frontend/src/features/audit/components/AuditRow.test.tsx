/**
 * AuditRow — chip colour per category, system actor label, expand state.
 */
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { Table, TableBody } from '@/components/ui/table';

import type { AuditEvent } from '../types';

import { AuditRow } from './AuditRow';

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

function renderRow(props: Partial<Parameters<typeof AuditRow>[0]> = {}) {
  const merged = {
    event: makeEvent(),
    actorName: 'alice',
    expanded: false,
    onToggle: vi.fn(),
    colSpan: 5,
    ...props,
  };
  return render(
    <Table>
      <TableBody>
        <AuditRow {...merged} />
      </TableBody>
    </Table>,
  );
}

describe('AuditRow', () => {
  it('renders the actor name when user_id is set', () => {
    renderRow({ event: makeEvent({ user_id: '22222222-2222-2222-2222-222222222222' }) });
    expect(screen.getByText('alice')).toBeInTheDocument();
  });

  it('renders "(system)" when user_id is null', () => {
    renderRow({ event: makeEvent({ user_id: null }), actorName: null });
    expect(screen.getByText('(system)')).toBeInTheDocument();
  });

  it('renders the action chip with the category-coloured palette', () => {
    renderRow({ event: makeEvent({ action: 'order.created' }) });
    const chip = screen.getByTestId('action-chip');
    expect(chip.className).toContain('bg-emerald-100');
  });

  it('renders schedule.* actions with violet chip', () => {
    renderRow({ event: makeEvent({ action: 'schedule.run' }) });
    expect(screen.getByTestId('action-chip').className).toContain('bg-violet-100');
  });

  it('formats the time column as YYYY-MM-DD HH:MM:SS UTC', () => {
    renderRow({ event: makeEvent({ created_at: '2026-05-20T14:32:10Z' }) });
    expect(screen.getByText('2026-05-20 14:32:10 UTC')).toBeInTheDocument();
  });

  it('renders resource as type/<short-uuid>… and infers type from action prefix', () => {
    renderRow({
      event: makeEvent({
        action: 'order.updated',
        resource_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
      }),
    });
    // first 8 chars + '…'
    expect(screen.getByText(/order\/aaaaaaaa…/)).toBeInTheDocument();
  });

  it('does not render detail row when collapsed', () => {
    renderRow({ expanded: false });
    expect(screen.queryByTestId(/audit-row-detail-/)).not.toBeInTheDocument();
  });

  it('renders detail row with full IDs when expanded', () => {
    renderRow({ expanded: true });
    const detail = screen.getByTestId(/audit-row-detail-/);
    expect(detail).toBeInTheDocument();
    expect(within(detail).getByText(/Event ID:/)).toBeInTheDocument();
    expect(within(detail).getByText(/Resource ID:/)).toBeInTheDocument();
    expect(within(detail).getByText(/Actor ID:/)).toBeInTheDocument();
  });

  it('calls onToggle when the row is clicked', async () => {
    const onToggle = vi.fn();
    const { default: userEvent } = await import('@testing-library/user-event');
    const user = userEvent.setup();
    renderRow({ onToggle });
    await user.click(screen.getByTestId(/^audit-row-/));
    expect(onToggle).toHaveBeenCalled();
  });
});
