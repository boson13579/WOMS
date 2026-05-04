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
