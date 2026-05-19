/**
 * Sparkline — verifies the warm-up guard (early-return null when the
 * ring buffer has < 3 samples) and the happy-path render via the mocked
 * Recharts ResponsiveContainer.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Sparkline } from './Sparkline';

describe('Sparkline', () => {
  it('returns null when the buffer has fewer than 3 samples', () => {
    // [RED] zero / one / two samples must NOT render a chart.
    const { container: empty } = render(<Sparkline values={[]} />);
    expect(empty).toBeEmptyDOMElement();

    const { container: one } = render(<Sparkline values={[10]} />);
    expect(one).toBeEmptyDOMElement();

    const { container: two } = render(<Sparkline values={[10, 12]} />);
    expect(two).toBeEmptyDOMElement();
  });

  it('renders the chart shell when the buffer has 3+ samples', () => {
    render(<Sparkline values={[10, 12, 14]} ariaLabel="Rate over time" />);
    expect(screen.getByTestId('sparkline')).toBeInTheDocument();
    // ResponsiveContainer is stubbed in src/test/setup.ts → div with
    // `data-testid="responsive-container"` mounts its children.
    expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    expect(screen.getByLabelText('Rate over time')).toBeInTheDocument();
  });
});
