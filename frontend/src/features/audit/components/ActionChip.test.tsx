/**
 * ActionChip — colour palette mapping per action category.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ActionChip } from './ActionChip';

describe('ActionChip', () => {
  it('renders user.* actions with the sky tone', () => {
    render(<ActionChip action="user.login_succeeded" />);
    const chip = screen.getByTestId('action-chip');
    expect(chip).toHaveTextContent('user.login_succeeded');
    expect(chip.className).toContain('bg-sky-100');
    expect(chip.className).toContain('text-sky-700');
  });

  it('renders order.* actions with the emerald tone', () => {
    render(<ActionChip action="order.created" />);
    const chip = screen.getByTestId('action-chip');
    expect(chip.className).toContain('bg-emerald-100');
    expect(chip.className).toContain('text-emerald-700');
  });

  it('renders schedule.* actions with the violet tone', () => {
    render(<ActionChip action="schedule.run" />);
    const chip = screen.getByTestId('action-chip');
    expect(chip.className).toContain('bg-violet-100');
    expect(chip.className).toContain('text-violet-700');
  });

  it('renders unknown actions with a muted tone', () => {
    render(<ActionChip action="weird.event" />);
    const chip = screen.getByTestId('action-chip');
    expect(chip.className).toContain('bg-muted');
    expect(chip.className).toContain('text-muted-foreground');
  });

  it('passes through extra className overrides', () => {
    render(<ActionChip action="user.created" className="custom-x" />);
    const chip = screen.getByTestId('action-chip');
    expect(chip.className).toContain('custom-x');
  });
});
