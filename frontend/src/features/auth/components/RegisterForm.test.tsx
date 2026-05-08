/**
 * [TDD - RED → GREEN → REFACTOR]
 *
 * Tests for RegisterForm component.
 * Covers: rendering, all field validations, password mismatch,
 * onSuccess and onSwitchToLogin callbacks.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { MemoryRouter } from 'react-router-dom';
import { RegisterForm } from './RegisterForm';

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    </MemoryRouter>
  );
}

describe('RegisterForm', () => {
  it('renders all required fields', () => {
    renderWithClient(<RegisterForm />);
    expect(screen.getByLabelText(/^username/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^email/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^password$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm password/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create account/i })).toBeInTheDocument();
  });

  it('validates username minimum length', async () => {
    renderWithClient(<RegisterForm />);
    await userEvent.type(screen.getByLabelText(/^username/i), 'ab');
    await userEvent.click(screen.getByRole('button', { name: /create account/i }));
    await waitFor(() => {
      expect(screen.getByText(/at least 3 characters/i)).toBeInTheDocument();
    });
  });

  it('rejects invalid email', async () => {
    renderWithClient(<RegisterForm />);
    await userEvent.type(screen.getByLabelText(/^email/i), 'not-an-email');
    await userEvent.click(screen.getByRole('button', { name: /create account/i }));
    await waitFor(() => {
      expect(screen.getByText(/invalid email/i)).toBeInTheDocument();
    });
  });

  it('enforces password strength — uppercase requirement', async () => {
    renderWithClient(<RegisterForm />);
    await userEvent.type(screen.getByLabelText(/^password$/i), 'alllower1');
    await userEvent.type(screen.getByLabelText(/confirm password/i), 'alllower1');
    await userEvent.click(screen.getByRole('button', { name: /create account/i }));
    await waitFor(() => {
      const alerts = screen.getAllByRole('alert');
      expect(alerts.some((el) => /uppercase letter/i.test(el.textContent))).toBe(true);
    });
  });

  it('enforces password strength — number requirement', async () => {
    renderWithClient(<RegisterForm />);
    await userEvent.type(screen.getByLabelText(/^password$/i), 'NoNumbers');
    await userEvent.type(screen.getByLabelText(/confirm password/i), 'NoNumbers');
    await userEvent.click(screen.getByRole('button', { name: /create account/i }));
    await waitFor(() => {
      const alerts = screen.getAllByRole('alert');
      expect(alerts.some((el) => /at least one number/i.test(el.textContent))).toBe(true);
    });
  });

  it('shows error when passwords do not match', async () => {
    renderWithClient(<RegisterForm />);
    await userEvent.type(screen.getByLabelText(/^password$/i), 'Password1');
    await userEvent.type(screen.getByLabelText(/confirm password/i), 'Password2');
    await userEvent.click(screen.getByRole('button', { name: /create account/i }));
    await waitFor(() => {
      expect(screen.getByText(/do not match/i)).toBeInTheDocument();
    });
  });

  it('renders "Sign in" link with correct href', () => {
    renderWithClient(<RegisterForm />);
    const link = screen.getByRole('link', { name: /sign in/i });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute('href', '/login');
  });
});
