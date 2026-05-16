import { isValidElement } from 'react';
import { describe, expect, it } from 'vitest';

import { ProtectedRoute } from '@/components/layout/ProtectedRoute';

import { routes } from './router';

describe('router protected routes', () => {
  it('keeps feature pages behind ProtectedRoute', () => {
    const rootRoute = routes.find((route) => route.path === '/');
    const rootElement = rootRoute?.element;

    expect(rootRoute).toBeDefined();
    expect(isValidElement(rootElement)).toBe(true);
    if (!isValidElement(rootElement)) {
      throw new Error('Root route element must be a React element.');
    }
    expect(rootElement.type).toBe(ProtectedRoute);
  });
});
