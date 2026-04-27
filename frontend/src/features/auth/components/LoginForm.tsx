/**
 * Login form skeleton — Phase 1.
 *
 * Demonstrates the Bulletproof React feature pattern (component lives in
 * `features/<feature>/components/`, API client in `features/<feature>/api/`)
 * with React Hook Form + Zod for form state and React Query for the mutation.
 *
 * No real authentication occurs — `login()` is a mock that resolves to a
 * fake token. Phase 2 wires this to the FastAPI auth endpoint.
 */
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation } from '@tanstack/react-query';
import { useForm } from 'react-hook-form';

import { Button } from '@/components/ui/button';

import { login, loginRequestSchema, type LoginRequest } from '../api/login';

export function LoginForm(): JSX.Element {
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<LoginRequest>({
    resolver: zodResolver(loginRequestSchema),
    defaultValues: { username: '', password: '' },
  });

  const mutation = useMutation({
    mutationFn: login,
    // Phase 2: persist the token in a Zustand store and redirect to /orders.
    onSuccess: (data) => {
      // eslint-disable-next-line no-console
      console.info('[Phase 1 mock] login succeeded', data);
    },
  });

  const onSubmit = handleSubmit((values) => {
    mutation.mutate(values);
  });

  return (
    <form
      onSubmit={(event) => {
        void onSubmit(event);
      }}
      className="space-y-4 rounded-lg border bg-card p-6 shadow-sm"
      noValidate
    >
      <div className="space-y-2">
        <label htmlFor="username" className="block text-sm font-medium">
          Username
          <input
            id="username"
            type="text"
            autoComplete="username"
            className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            {...register('username')}
          />
        </label>
        {errors.username ? (
          <p className="text-xs text-destructive">{errors.username.message}</p>
        ) : null}
      </div>

      <div className="space-y-2">
        <label htmlFor="password" className="block text-sm font-medium">
          Password
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-normal focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            {...register('password')}
          />
        </label>
        {errors.password ? (
          <p className="text-xs text-destructive">{errors.password.message}</p>
        ) : null}
      </div>

      <Button type="submit" disabled={mutation.isPending} className="w-full">
        {mutation.isPending ? 'Signing in…' : 'Sign in'}
      </Button>

      {mutation.isError ? (
        <p className="text-xs text-destructive">Login failed. Please try again.</p>
      ) : null}
    </form>
  );
}
