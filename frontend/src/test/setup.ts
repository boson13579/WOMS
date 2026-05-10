/**
 * Vitest global setup.
 *
 * 1. Extend `expect` with @testing-library/jest-dom matchers
 *    (`toBeInTheDocument`, `toHaveTextContent`, ...).
 * 2. Stub Recharts' `ResponsiveContainer`: jsdom has no real layout engine,
 *    so the container would yield a 0x0 box and the chart would never render.
 *    Replacing it with a fixed-size div lets descendants mount and lets us
 *    assert "a chart exists" without needing pixel-perfect output.
 */
import '@testing-library/jest-dom/vitest';

import * as React from 'react';
import type * as recharts from 'recharts';
import { vi } from 'vitest';

// jsdom does not implement HTMLDialogElement.showModal / close.
// Polyfill so any component using native <dialog> can be tested.
HTMLDialogElement.prototype.showModal = vi.fn(function showModal(this: HTMLDialogElement) {
  this.setAttribute('open', '');
});
HTMLDialogElement.prototype.close = vi.fn(function close(this: HTMLDialogElement) {
  this.removeAttribute('open');
  this.dispatchEvent(new Event('close'));
});

vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof recharts>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) =>
      React.createElement(
        'div',
        { 'data-testid': 'responsive-container', style: { width: 400, height: 200 } },
        children,
      ),
  };
});

// Mock fetch for all React Query mutations in components
global.fetch = vi.fn(async (url: RequestInfo | URL) => {
  // Artificial delay to test loading states
  await new Promise((resolve) => {
    setTimeout(resolve, 50);
  });

  if (url === '/api/v1/auth/login') {
    return new Response(JSON.stringify({ access_token: 'mock-token', token_type: 'bearer' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }
  if (url === '/api/v1/auth/register') {
    return new Response(
      JSON.stringify({
        id: '123e4567-e89b-12d3-a456-426614174000',
        username: 'testuser',
        email: 'test@example.com',
        role: 'viewer',
        is_active: true,
        version_id: 1,
        created_at: new Date().toISOString(),
      }),
      { status: 201, headers: { 'Content-Type': 'application/json' } },
    );
  }
  return new Response('Not Found', { status: 404 });
}) as unknown as typeof fetch;
