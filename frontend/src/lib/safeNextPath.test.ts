/**
 * Tests for ``safeNextPath`` — guards the ``?next=`` propagation contract
 * against open-redirect-shaped inputs.
 */
import { describe, expect, it } from 'vitest';

import { safeNextPath } from './safeNextPath';

describe('safeNextPath', () => {
  it('returns / for null', () => {
    expect(safeNextPath(null)).toBe('/');
  });

  it('returns / for an empty string', () => {
    expect(safeNextPath('')).toBe('/');
  });

  it('passes a clean absolute path through unchanged', () => {
    expect(safeNextPath('/orders')).toBe('/orders');
  });

  it('passes a path with query string through unchanged', () => {
    expect(safeNextPath('/orders?status=open')).toBe('/orders?status=open');
  });

  it('rejects a protocol-relative URL', () => {
    expect(safeNextPath('//evil.example')).toBe('/');
  });

  it('rejects an absolute http URL', () => {
    expect(safeNextPath('https://evil.example')).toBe('/');
  });

  it('rejects a javascript: pseudo-URL', () => {
    // eslint-disable-next-line no-script-url
    expect(safeNextPath('javascript:alert(1)')).toBe('/');
  });

  it('rejects a relative path without a leading slash', () => {
    expect(safeNextPath('evil')).toBe('/');
  });

  it('rejects a relative path with dot-segments', () => {
    expect(safeNextPath('../admin')).toBe('/');
  });
});
