/**
 * TimeRangeSelector — segmented pill picker.
 *
 * Covers (a) rendering of the four pills, (b) ``aria-pressed`` matches
 * the active value, and (c) clicking a non-active pill fires
 * ``onChange`` with the right ``seconds`` value.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { TimeRangeSelector } from './TimeRangeSelector';

describe('TimeRangeSelector', () => {
  it('renders all four time-range pills', () => {
    render(<TimeRangeSelector value={60} onChange={vi.fn()} />);
    expect(screen.getByRole('button', { name: '1m' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '5m' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '15m' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '1h' })).toBeInTheDocument();
  });

  it('marks the active value with aria-pressed=true', () => {
    render(<TimeRangeSelector value={300} onChange={vi.fn()} />);
    expect(screen.getByRole('button', { name: '5m' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '1m' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('calls onChange with the right seconds value on click', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<TimeRangeSelector value={60} onChange={onChange} />);

    await user.click(screen.getByRole('button', { name: '15m' }));
    expect(onChange).toHaveBeenCalledWith(900);

    await user.click(screen.getByRole('button', { name: '1h' }));
    expect(onChange).toHaveBeenCalledWith(3600);
  });
});
