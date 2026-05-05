/**
 * [TDD - RED → GREEN → REFACTOR]
 *
 * Tests for LoginForm component.
 * Covers: rendering, validation errors, submission flow, loading state.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { LoginForm } from './LoginForm';

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe('LoginForm', () => {
  it('renders username and password fields', () => {
    renderWithClient(<LoginForm />);
    expect(screen.getByLabelText(/username/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument();
  });

  it('shows validation error when username is empty', async () => {
    renderWithClient(<LoginForm />);
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));
    await waitFor(() => {
      expect(screen.getByText(/username is required/i)).toBeInTheDocument();
    });
  });

  it('shows validation error when password is too short', async () => {
    renderWithClient(<LoginForm />);
    await userEvent.type(screen.getByLabelText(/username/i), 'testuser');
    await userEvent.type(screen.getByLabelText(/password/i), 'short');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));
    await waitFor(() => {
      expect(screen.getByText(/at least 8 characters/i)).toBeInTheDocument();
    });
  });

  it('calls onSwitchToRegister when "Create one" is clicked', async () => {
    const onSwitch = vi.fn();
    renderWithClient(<LoginForm onSwitchToRegister={onSwitch} />);
    await userEvent.click(screen.getByRole('button', { name: /create one/i }));
    expect(onSwitch).toHaveBeenCalledOnce();
  });

  it('shows loading state during submission', async () => {
    renderWithClient(<LoginForm />);
    await userEvent.type(screen.getByLabelText(/username/i), 'testuser');
    await userEvent.type(screen.getByLabelText(/password/i), 'Password1');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));
    await waitFor(() => {
      expect(screen.getByText(/signing in/i)).toBeInTheDocument();
    });
  });
});
