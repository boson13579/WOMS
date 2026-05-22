/**
 * AuditPagination — boundary handling and page-size change.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { AuditPagination } from './AuditPagination';

describe('AuditPagination', () => {
  it('disables First / Prev on page 1', () => {
    render(
      <AuditPagination
        page={1}
        pageSize={20}
        total={500}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );
    expect(screen.getByRole('button', { name: 'First page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next page' })).not.toBeDisabled();
    expect(screen.getByRole('button', { name: 'Last page' })).not.toBeDisabled();
  });

  it('disables Next / Last on the last page', () => {
    // total=80 with pageSize=20 → totalPages=4
    render(
      <AuditPagination
        page={4}
        pageSize={20}
        total={80}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Last page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'First page' })).not.toBeDisabled();
    expect(screen.getByRole('button', { name: 'Previous page' })).not.toBeDisabled();
  });

  it('calls onPageChange with the next page when Next is clicked', async () => {
    const onPageChange = vi.fn();
    const user = userEvent.setup();
    render(
      <AuditPagination
        page={2}
        pageSize={20}
        total={500}
        onPageChange={onPageChange}
        onPageSizeChange={vi.fn()}
      />,
    );
    await user.click(screen.getByRole('button', { name: 'Next page' }));
    expect(onPageChange).toHaveBeenCalledWith(3);
  });

  it('calls onPageChange with the previous page when Prev is clicked', async () => {
    const onPageChange = vi.fn();
    const user = userEvent.setup();
    render(
      <AuditPagination
        page={5}
        pageSize={20}
        total={500}
        onPageChange={onPageChange}
        onPageSizeChange={vi.fn()}
      />,
    );
    await user.click(screen.getByRole('button', { name: 'Previous page' }));
    expect(onPageChange).toHaveBeenCalledWith(4);
  });

  it('calls onPageSizeChange when the page-size select changes', async () => {
    const onPageSizeChange = vi.fn();
    const user = userEvent.setup();
    render(
      <AuditPagination
        page={1}
        pageSize={20}
        total={500}
        onPageChange={vi.fn()}
        onPageSizeChange={onPageSizeChange}
      />,
    );
    await user.selectOptions(screen.getByLabelText('Page size'), '50');
    expect(onPageSizeChange).toHaveBeenCalledWith(50);
  });

  it('renders "Page X of Y · Z total events" indicator', () => {
    render(
      <AuditPagination
        page={3}
        pageSize={20}
        total={935}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );
    // totalPages = ceil(935 / 20) = 47
    expect(screen.getByText(/Page 3 of 47/)).toBeInTheDocument();
    expect(screen.getByText(/935 total events/)).toBeInTheDocument();
  });

  it('shows totalPages=1 when total is zero', () => {
    render(
      <AuditPagination
        page={1}
        pageSize={20}
        total={0}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );
    expect(screen.getByText(/Page 1 of 1/)).toBeInTheDocument();
    // All nav buttons disabled because page=1 and totalPages=1
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
  });
});
