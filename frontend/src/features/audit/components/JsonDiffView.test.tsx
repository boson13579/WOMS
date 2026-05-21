/**
 * JsonDiffView — old/new value rendering rules.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { JsonDiffView } from './JsonDiffView';

describe('JsonDiffView', () => {
  it('renders "(no field details)" when both values are null', () => {
    render(<JsonDiffView oldValue={null} newValue={null} />);
    expect(screen.getByTestId('json-diff-empty')).toHaveTextContent('(no field details)');
    expect(screen.queryByTestId('json-diff')).not.toBeInTheDocument();
  });

  it('renders side-by-side panels when old + new are flat objects', () => {
    render(
      <JsonDiffView
        oldValue={{ status: 'pending', quantity: 100 }}
        newValue={{ status: 'scheduled', quantity: 100 }}
      />,
    );
    expect(screen.getByTestId('json-panel-before')).toBeInTheDocument();
    expect(screen.getByTestId('json-panel-after')).toBeInTheDocument();
    expect(screen.getByTestId('json-panel-before')).toHaveTextContent('Before');
    expect(screen.getByTestId('json-panel-after')).toHaveTextContent('After');
    expect(screen.getByTestId('json-panel-before')).toHaveTextContent('pending');
    expect(screen.getByTestId('json-panel-after')).toHaveTextContent('scheduled');
  });

  it('renders only one side full-width when the other is null (creation case)', () => {
    render(<JsonDiffView oldValue={null} newValue={{ name: 'new-order' }} />);
    const before = screen.getByTestId('json-panel-before');
    expect(before).toHaveTextContent('—');
    const after = screen.getByTestId('json-panel-after');
    expect(after).toHaveTextContent('new-order');
  });

  it('masks sensitive keys (password, token, secret)', () => {
    render(
      <JsonDiffView
        oldValue={{ username: 'alice', password: 's3cret' }}
        newValue={{ username: 'alice', api_token: 'tok-xyz' }}
      />,
    );
    const before = screen.getByTestId('json-panel-before');
    const after = screen.getByTestId('json-panel-after');
    expect(before).toHaveTextContent('••••');
    expect(before).not.toHaveTextContent('s3cret');
    expect(after).toHaveTextContent('••••');
    expect(after).not.toHaveTextContent('tok-xyz');
  });

  it('serialises nested objects via JSON.stringify', () => {
    render(
      <JsonDiffView
        oldValue={{ meta: { foo: 1, bar: 2 } }}
        newValue={{ meta: { foo: 3, bar: 2 } }}
      />,
    );
    const before = screen.getByTestId('json-panel-before');
    // JSON.stringify with indent emits 2-space indent and quotes
    expect(before.textContent).toContain('"foo": 1');
  });

  it('shows (empty object) for {} payloads', () => {
    render(<JsonDiffView oldValue={{}} newValue={{ x: 1 }} />);
    expect(screen.getByTestId('json-panel-before')).toHaveTextContent('(empty object)');
  });
});
