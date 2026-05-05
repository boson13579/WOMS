/**
 * AuthPage — full-screen auth shell that toggles between LoginForm and
 * RegisterForm. Uses a Zustand `AuthMode` client state (not server state)
 * to track which form is visible.
 *
 * Styling: dark gradient background, glassmorphism card, smooth fade
 * transition between forms — matches the premium design requirements.
 */
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { ThemeToggle } from '@/components/layout/ThemeToggle';

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';

import type { AuthMode } from '../types/auth';

import { LoginForm } from './LoginForm';
import { RegisterForm } from './RegisterForm';

interface AuthPageProps {
  /** Called after a successful login so the app shell can navigate to /dashboard. */
  onLoginSuccess?: () => void;
}

export function AuthPage({ onLoginSuccess }: AuthPageProps): JSX.Element {
  const [mode, setMode] = useState<AuthMode>('login');
  const [registered, setRegistered] = useState(false);
  const navigate = useNavigate();

  const handleLoginSuccess = () => {
    if (onLoginSuccess) {
      onLoginSuccess();
    } else {
      navigate('/');
    }
  };

  const handleRegisterSuccess = () => {
    setRegistered(true);
    setMode('login');
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-muted/40 p-4">
      <div className="absolute right-4 top-4">
        <ThemeToggle />
      </div>
      <div className="w-full max-w-md">
        {/* Logo / brand */}
        <div className="mb-8 text-center">
          <div className="mb-3 inline-flex h-12 w-12 items-center justify-center rounded-xl bg-primary shadow-lg shadow-primary/20">
            <svg
              aria-hidden="true"
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="white"
              className="h-6 w-6"
            >
              <path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h16v2H4z" />
            </svg>
          </div>
          <h1 className="text-3xl font-bold tracking-tight text-foreground">Smart Order</h1>
          <p className="mt-1 text-sm text-muted-foreground">Order management &amp; scheduling platform</p>
        </div>

        <Card className="shadow-md">
          <CardHeader className="space-y-1 pb-4">
            <CardTitle className="text-xl">
              {mode === 'login' ? 'Welcome back' : 'Create your account'}
            </CardTitle>
            <CardDescription>
              {mode === 'login'
                ? 'Sign in to access your dashboard'
                : 'Get started with Smart Order today'}
            </CardDescription>
          </CardHeader>

          <CardContent>
            {/* Success banner after registration */}
            {registered && mode === 'login' ? (
              <div
                role="status"
                className="mb-4 rounded-md border border-green-500/30 bg-green-500/10 px-4 py-3 text-sm text-green-600 dark:text-green-400"
              >
                Account created! Please sign in.
              </div>
            ) : null}

            {/* Animated form swap */}
            <div key={mode} className="animate-in fade-in duration-300">
              {mode === 'login' ? (
                <LoginForm
                  {...(handleLoginSuccess !== undefined ? { onSuccess: handleLoginSuccess } : {})}
                  onSwitchToRegister={() => {
                    setRegistered(false);
                    setMode('register');
                  }}
                />
              ) : (
                <RegisterForm
                  onSuccess={handleRegisterSuccess}
                  onSwitchToLogin={() => {
                    setMode('login');
                  }}
                />
              )}
            </div>
          </CardContent>
        </Card>

        <p className="mt-6 text-center text-xs text-muted-foreground">
          Smart Order Management System &copy; {new Date().getFullYear()}
        </p>
      </div>
    </div>
  );
}
