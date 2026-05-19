import { isValidElement } from 'react';
import { describe, expect, it } from 'vitest';

import { AuthOnlyRoute } from '@/components/layout/AuthOnlyRoute';
import { ProtectedRoute } from '@/components/layout/ProtectedRoute';
import { RoleProtectedRoute } from '@/components/layout/RoleProtectedRoute';
import { SessionBoundary } from '@/components/layout/SessionBoundary';

import { routes } from './router';

function findRoute(path: string) {
  // The router tree is now wrapped in a pathless SessionBoundary route at
  // index 0; the original top-level routes live in its ``children``.
  const root = routes[0];
  expect(root).toBeDefined();
  if (!root.children) {
    throw new Error('Expected SessionBoundary root to have children.');
  }
  return root.children.find((r) => r.path === path);
}

describe('router structure', () => {
  it('wraps the entire tree in a SessionBoundary pathless route', () => {
    const root = routes[0];
    expect(root).toBeDefined();
    const { element } = root;
    expect(isValidElement(element)).toBe(true);
    if (!isValidElement(element)) {
      throw new Error('SessionBoundary root must be a React element.');
    }
    expect(element.type).toBe(SessionBoundary);
  });

  it('keeps feature pages behind ProtectedRoute', () => {
    const rootRoute = findRoute('/');
    const rootElement = rootRoute?.element;

    expect(rootRoute).toBeDefined();
    expect(isValidElement(rootElement)).toBe(true);
    if (!isValidElement(rootElement)) {
      throw new Error('Root route element must be a React element.');
    }
    expect(rootElement.type).toBe(ProtectedRoute);
  });

  it('wraps /login and /register in AuthOnlyRoute', () => {
    const loginRoute = findRoute('/login');
    const registerRoute = findRoute('/register');

    expect(loginRoute).toBeDefined();
    expect(registerRoute).toBeDefined();
    expect(isValidElement(loginRoute?.element)).toBe(true);
    expect(isValidElement(registerRoute?.element)).toBe(true);
    if (!isValidElement(loginRoute?.element) || !isValidElement(registerRoute?.element)) {
      throw new Error('Auth route elements must be React elements.');
    }
    expect(loginRoute.element.type).toBe(AuthOnlyRoute);
    expect(registerRoute.element.type).toBe(AuthOnlyRoute);
  });

  it('gates /users behind a RoleProtectedRoute allowing only root', () => {
    // SessionBoundary → ProtectedRoute (path '/') → AppShell → role gate → users.
    const rootRoute = findRoute('/');
    const appShellRoute = rootRoute?.children?.[0];
    const shellChildren = appShellRoute?.children ?? [];

    // The role gate is a layout route (no path) whose children include
    // 'users'. Pluck it out and assert both its element type and the
    // ``allowedRoles`` prop.
    const roleGate = shellChildren.find((r) => r.children?.some((child) => child.path === 'users'));
    expect(roleGate).toBeDefined();
    const gateElement = roleGate?.element;
    expect(isValidElement(gateElement)).toBe(true);
    if (!isValidElement(gateElement)) {
      throw new Error('Role gate element must be a React element.');
    }
    expect(gateElement.type).toBe(RoleProtectedRoute);
    const props = gateElement.props as { allowedRoles?: string[] };
    expect(props.allowedRoles).toEqual(['root']);

    // The plain 'users' leaf should NOT be a direct sibling of orders.
    const sharesShellLevel = shellChildren.find((r) => r.path === 'users');
    expect(sharesShellLevel).toBeUndefined();
  });
});
