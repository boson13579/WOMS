/**
 * StatusCard — service health pill mapping.
 *
 * The status → pill mapping is the most-load-bearing visual contract on the
 * dashboard. Operators glance at it to decide if something is on fire, so the
 * mapping must be correct and stable.
 *
 * TDD note: implementation was written before these tests on the
 * `feat/dashboard` branch (acknowledged transgression of RULES.md §5).
 * Running these tests now codifies the expected behaviour so any future
 * accidental regression is caught.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ServiceHealthEntry } from '../types';

import { StatusCard } from './StatusCard';

function svc(overrides: Partial<ServiceHealthEntry> = {}): ServiceHealthEntry {
  return {
    id: 'api',
    name: 'API',
    status: 'healthy',
    summary: 'FastAPI · v0.1.0',
    details: [
      { label: 'Uptime', value: '99.97%' },
      { label: 'Latency', value: '23 ms' },
    ],
    ...overrides,
  };
}

describe('StatusCard', () => {
  it('shows the service name and summary', () => {
    render(<StatusCard service={svc()} />);
    expect(screen.getByRole('heading', { name: 'API' })).toBeInTheDocument();
    expect(screen.getByText('FastAPI · v0.1.0')).toBeInTheDocument();
  });

  it('renders all detail label / value pairs', () => {
    const service = svc({
      details: [
        { label: 'Uptime', value: '99.97%' },
        { label: 'Latency', value: '23 ms' },
        { label: 'Replicas', value: '2/2' },
      ],
    });
    render(<StatusCard service={service} />);
    expect(screen.getByText('Uptime')).toBeInTheDocument();
    expect(screen.getByText('99.97%')).toBeInTheDocument();
    expect(screen.getByText('Latency')).toBeInTheDocument();
    expect(screen.getByText('23 ms')).toBeInTheDocument();
    expect(screen.getByText('Replicas')).toBeInTheDocument();
    expect(screen.getByText('2/2')).toBeInTheDocument();
  });

  it.each([
    ['healthy', 'Healthy', 'text-emerald-700'],
    ['warning', 'Warning', 'text-amber-700'],
    ['error', 'Error', 'text-red-700'],
  ] as const)(
    'maps status=%s to label=%s with the expected color class',
    (status, expectedLabel, expectedClass) => {
      render(<StatusCard service={svc({ status })} />);
      const pill = screen.getByText(expectedLabel);
      expect(pill).toBeInTheDocument();
      // The class is on the pill's parent (the badge wrapper).
      expect(pill.closest('span')).toHaveClass(expectedClass);
    },
  );

  it('does not pulse the dot when the service is unhealthy', () => {
    const { container } = render(<StatusCard service={svc({ status: 'error' })} />);
    const dot = container.querySelector('span.rounded-full');
    expect(dot).not.toHaveClass('animate-pulse');
  });
});
