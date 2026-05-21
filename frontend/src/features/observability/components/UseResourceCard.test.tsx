/**
 * UseResourceCard — generic resource-utilization tile.
 *
 * Covers: bar width matches ratio, destructive fill when > 80%,
 * "unreachable" copy when value is ``null``, ratio dashed bar when
 * ratio is ``null``, and the optional ``expandable`` slot mounts when
 * supplied.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { UseResourceCard } from './UseResourceCard';

describe('UseResourceCard', () => {
  it('renders label, value, and a bar at the requested ratio', () => {
    render(
      <UseResourceCard label="DB connections" value="2 / 20" ratio={0.1} caption="10% used" />,
    );
    expect(screen.getByText('DB connections')).toBeInTheDocument();
    expect(screen.getByText('2 / 20')).toBeInTheDocument();
    expect(screen.getByText(/10% used/i)).toBeInTheDocument();
    const fill = screen.getByTestId('util-bar-fill');
    expect(fill).toHaveStyle({ width: '10%' });
    expect(fill.className).toContain('bg-primary');
  });

  it('switches to the destructive fill colour when ratio > 0.8', () => {
    render(<UseResourceCard label="DB connections" value="18 / 20" ratio={0.9} />);
    const fill = screen.getByTestId('util-bar-fill');
    expect(fill.className).toContain('bg-destructive');
  });

  it('renders the unreachable copy + "--" when value is null', () => {
    render(<UseResourceCard label="Redis memory" value={null} ratio={null} />);
    expect(screen.getByText('--')).toBeInTheDocument();
    expect(screen.getByText(/probe unreachable/i)).toBeInTheDocument();
  });

  it('renders the dashed bar pattern when ratio is null but value is set', () => {
    render(<UseResourceCard label="Redis memory" value="12 MB" ratio={null} />);
    expect(screen.getByTestId('util-bar-dashed')).toBeInTheDocument();
  });

  it('renders an expandable slot when provided', () => {
    render(
      <UseResourceCard
        label="Workers"
        value="5 up"
        ratio={0.3}
        expandable={<div data-testid="expandable-content">drill-down</div>}
      />,
    );
    expect(screen.getByTestId('expandable-content')).toBeInTheDocument();
  });
});
