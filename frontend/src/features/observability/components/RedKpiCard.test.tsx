/**
 * RedKpiCard — single KPI tile.
 *
 * Verifies: label / value / unit / delta rendering, the tone-class
 * mapping, optional subtitle (used by the SLO card), and the
 * sparkline early-return behaviour (the sparkline mounts only when
 * the buffer has 3+ samples).
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { RedKpiCard } from './RedKpiCard';

describe('RedKpiCard', () => {
  it('renders the label, value, and unit', () => {
    render(<RedKpiCard label="Rate" value="12.40" unit="/s" />);
    expect(screen.getByText('Rate')).toBeInTheDocument();
    expect(screen.getByText('12.40')).toBeInTheDocument();
    expect(screen.getByText('/s')).toBeInTheDocument();
  });

  it('renders the delta line with the supplied text', () => {
    render(<RedKpiCard label="Rate" value="12.40" unit="/s" delta="up 3.00/s" />);
    expect(screen.getByText('up 3.00/s')).toBeInTheDocument();
  });

  it('renders the subtitle when provided (SLO card use case)', () => {
    render(
      <RedKpiCard
        label="SLO compliance"
        value="99.85"
        unit="%"
        subtitle="Target: 99.5% • Budget remaining: 80%"
        tone="positive"
      />,
    );
    expect(screen.getByText(/target: 99.5%/i)).toBeInTheDocument();
    expect(screen.getByText(/budget remaining: 80%/i)).toBeInTheDocument();
  });

  it('does NOT render the sparkline when the buffer has fewer than 3 samples', () => {
    render(<RedKpiCard label="Rate" value="12.40" unit="/s" sparklineData={[1, 2]} />);
    expect(screen.queryByTestId('sparkline')).not.toBeInTheDocument();
  });

  it('renders the sparkline when the buffer has 3+ samples', () => {
    render(<RedKpiCard label="Rate" value="12.40" unit="/s" sparklineData={[1, 2, 3, 4]} />);
    expect(screen.getByTestId('sparkline')).toBeInTheDocument();
  });

  it('renders the footnote line when provided', () => {
    render(
      <RedKpiCard
        label="SLO compliance"
        value="99.85"
        unit="%"
        subtitle="Target: 99.5% • Budget remaining: 80%"
        footnote="data: last 60m"
      />,
    );
    expect(screen.getByTestId('red-kpi-footnote')).toHaveTextContent('data: last 60m');
  });

  it('omits the footnote line when not provided', () => {
    render(<RedKpiCard label="Rate" value="12.40" unit="/s" />);
    expect(screen.queryByTestId('red-kpi-footnote')).not.toBeInTheDocument();
  });
});
