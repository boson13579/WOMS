/**
 * LoginForm — Phase 2-ready login form.
 *
 * Uses React Hook Form + Zod for validation, and the `useLogin` React Query
 * mutation. On success, persists the token in the Zustand auth store and calls
 * `onSuccess` so the parent (AuthPage) can switch view.
 */
import { zodResolver } from '@hookform/resolvers/zod';
import { Loader2, LogIn } from 'lucide-react';
import { useForm } from 'react-hook-form';
import { Link } from 'react-router-dom';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

import { loginRequestSchema, useLogin, type LoginRequest } from '../api/auth';
import { useAuthStore } from '../stores/authStore';

interface LoginFormProps {
  /** Called when login succeeds — parent uses this to navigate. */
  onSuccess?: () => void;
  /** Called when user wants to switch to the register form. */
  onSwitchToRegister?: () => void;
}

export function LoginForm({ onSuccess, onSwitchToRegister }: LoginFormProps): JSX.Element {
  const setToken = useAuthStore((s) => s.setToken);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<LoginRequest>({
    resolver: zodResolver(loginRequestSchema),
    defaultValues: { username: '', password: '' },
  });

  const mutation = useLogin();

  const onSubmit = handleSubmit((values) => {
    mutation.mutate(values, {
      onSuccess: (data) => {
        setToken(data.access_token, values.username);
        onSuccess?.();
      },
    });
  });

  return (
    <form
      onSubmit={(event) => {
        void onSubmit(event);
      }}
      className="space-y-5"
      noValidate
    >
      <div className="space-y-2">
        <Label htmlFor="login-username">Username</Label>
        <Input
          id="login-username"
          type="text"
          autoComplete="username"
          placeholder="your_username"
          aria-invalid={errors.username !== undefined}
          aria-describedby={errors.username ? 'login-username-error' : undefined}
          {...register('username')}
        />
        {errors.username ? (
          <p id="login-username-error" className="text-xs text-destructive" role="alert">
            {errors.username.message}
          </p>
        ) : null}
      </div>

      <div className="space-y-2">
        <Label htmlFor="login-password">Password</Label>
        <Input
          id="login-password"
          type="password"
          autoComplete="current-password"
          placeholder="••••••••"
          aria-invalid={errors.password !== undefined}
          aria-describedby={errors.password ? 'login-password-error' : undefined}
          {...register('password')}
        />
        {errors.password ? (
          <p id="login-password-error" className="text-xs text-destructive" role="alert">
            {errors.password.message}
          </p>
        ) : null}
      </div>

      {mutation.isError ? (
        <p className="text-xs text-destructive" role="alert">
          Login failed. Please check your credentials and try again.
        </p>
      ) : null}

      <Button type="submit" disabled={mutation.isPending} className="w-full">
        {mutation.isPending ? (
          <>
            <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
            Signing in…
          </>
        ) : (
          <>
            <LogIn className="mr-2 h-4 w-4" aria-hidden="true" />
            Sign in
          </>
        )}
      </Button>

        <p className="text-center text-sm text-muted-foreground">
          Don&apos;t have an account?{' '}
          <Link
            to="/register"
            className="font-medium text-primary underline-offset-4 hover:underline"
          >
            Create one
          </Link>
        </p>
    </form>
  );
}
