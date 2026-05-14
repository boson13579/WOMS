/**
 * ServiceHealthGrid — wraps StatusCard with loading / error states.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { SystemHealthResponse } from '../types';

import { ServiceHealthGrid } from './ServiceHealthGrid';

function makeData(): SystemHealthResponse {
  return {
    services: [
      {
        id: 'api',
        name: 'API',
        status: 'healthy',
        summary: 'FastAPI · v0.1.0',
        details: [{ label: 'Version', value: '0.1.0' }],
      },
      {
        id: 'postgres',
        name: 'PostgreSQL',
        status: 'healthy',
        summary: 'postgres:15-alpine',
        details: [{ label: 'Latency', value: '2 ms' }],
      },
      {
        id: 'redis',
        name: 'Redis',
        status: 'warning',
        summary: 'redis:7-alpine',
        details: [{ label: 'Latency', value: '1 ms' }],
      },
      {
        id: 'celery',
        name: 'Celery Worker',
        status: 'error',
        summary: 'Unable to read scheduler state',
        details: [{ label: 'Error', value: 'connection refused' }],
      },
    ],
  };
}

describe('ServiceHealthGrid', () => {
  it('renders one StatusCard per service entry', () => {
    render(<ServiceHealthGrid data={makeData()} isLoading={false} isError={false} />);
    expect(screen.getByRole('heading', { name: 'API' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'PostgreSQL' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Redis' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Celery Worker' })).toBeInTheDocument();
  });

  it('shows skeleton placeholders while loading', () => {
    render(<ServiceHealthGrid data={undefined} isLoading isError={false} />);
    expect(screen.getAllByTestId('service-health-skeleton')).toHaveLength(4);
  });

  it('shows error message when isError', () => {
    render(<ServiceHealthGrid data={undefined} isLoading={false} isError />);
    expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
  });
});
