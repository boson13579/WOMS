/**
 * RegisterForm — new user registration.
 *
 * Uses React Hook Form + Zod (password strength + confirmation validation),
 * and the `useRegister` React Query mutation. On success, calls `onSuccess`
 * so the parent (AuthPage) can switch to the login form.
 */
import { zodResolver } from '@hookform/resolvers/zod';
import { Loader2, UserPlus } from 'lucide-react';
import { useForm } from 'react-hook-form';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

import { registerRequestSchema, useRegister, type RegisterRequest } from '../api/auth';

interface RegisterFormProps {
  /** Called when registration succeeds — parent switches to login view. */
  onSuccess?: () => void;
  /** Called when user wants to switch back to the login form. */
  onSwitchToLogin?: () => void;
}

export function RegisterForm({ onSuccess, onSwitchToLogin }: RegisterFormProps): JSX.Element {
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<RegisterRequest>({
    resolver: zodResolver(registerRequestSchema),
    defaultValues: { username: '', email: '', password: '', confirmPassword: '' },
  });

  const mutation = useRegister();

  const onSubmit = handleSubmit((values) => {
    mutation.mutate(values, {
      onSuccess: () => {
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
      {/* Username */}
      <div className="space-y-2">
        <Label htmlFor="register-username">Username</Label>
        <Input
          id="register-username"
          type="text"
          autoComplete="username"
          placeholder="your_username"
          aria-invalid={errors.username !== undefined}
          aria-describedby={errors.username ? 'register-username-error' : undefined}
          {...register('username')}
        />
        {errors.username ? (
          <p id="register-username-error" className="text-xs text-destructive" role="alert">
            {errors.username.message}
          </p>
        ) : null}
      </div>

      {/* Email */}
      <div className="space-y-2">
        <Label htmlFor="register-email">Email</Label>
        <Input
          id="register-email"
          type="email"
          autoComplete="email"
          placeholder="you@example.com"
          aria-invalid={errors.email !== undefined}
          aria-describedby={errors.email ? 'register-email-error' : undefined}
          {...register('email')}
        />
        {errors.email ? (
          <p id="register-email-error" className="text-xs text-destructive" role="alert">
            {errors.email.message}
          </p>
        ) : null}
      </div>

      {/* Password */}
      <div className="space-y-2">
        <Label htmlFor="register-password">Password</Label>
        <Input
          id="register-password"
          type="password"
          autoComplete="new-password"
          placeholder="••••••••"
          aria-invalid={errors.password !== undefined}
          aria-describedby={errors.password ? 'register-password-error' : undefined}
          {...register('password')}
        />
        {errors.password ? (
          <p id="register-password-error" className="text-xs text-destructive" role="alert">
            {errors.password.message}
          </p>
        ) : null}
        <p className="text-xs text-muted-foreground">
          Min 8 characters, one uppercase letter, one number.
        </p>
      </div>

      {/* Confirm Password */}
      <div className="space-y-2">
        <Label htmlFor="register-confirm-password">Confirm Password</Label>
        <Input
          id="register-confirm-password"
          type="password"
          autoComplete="new-password"
          placeholder="••••••••"
          aria-invalid={errors.confirmPassword !== undefined}
          aria-describedby={errors.confirmPassword ? 'register-confirm-password-error' : undefined}
          {...register('confirmPassword')}
        />
        {errors.confirmPassword ? (
          <p id="register-confirm-password-error" className="text-xs text-destructive" role="alert">
            {errors.confirmPassword.message}
          </p>
        ) : null}
      </div>

      {mutation.isError ? (
        <p className="text-xs text-destructive" role="alert">
          Registration failed. Please try again.
        </p>
      ) : null}

      <Button type="submit" disabled={mutation.isPending} className="w-full">
        {mutation.isPending ? (
          <>
            <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
            Creating account…
          </>
        ) : (
          <>
            <UserPlus className="mr-2 h-4 w-4" aria-hidden="true" />
            Create account
          </>
        )}
      </Button>

      {onSwitchToLogin ? (
        <p className="text-center text-sm text-muted-foreground">
          Already have an account?{' '}
          <button
            type="button"
            onClick={onSwitchToLogin}
            className="font-medium text-primary underline-offset-4 hover:underline"
          >
            Sign in
          </button>
        </p>
      ) : null}
    </form>
  );
}
