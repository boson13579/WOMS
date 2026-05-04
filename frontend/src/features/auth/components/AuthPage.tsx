/**
 * AuthPage — full-screen auth shell that toggles between LoginForm and
 * RegisterForm. Uses a Zustand `AuthMode` client state (not server state)
 * to track which form is visible.
 *
 * Styling: dark gradient background, glassmorphism card, smooth fade
 * transition between forms — matches the premium design requirements.
 */
import { useState } from 'react';

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

  const handleRegisterSuccess = () => {
    setRegistered(true);
    setMode('login');
  };

  return (
    <div className="relative min-h-screen overflow-hidden bg-gradient-to-br from-slate-900 via-purple-950 to-slate-900 flex items-center justify-center p-4">
      {/* Decorative blurred orbs */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-40 -left-40 h-96 w-96 rounded-full bg-purple-600/30 blur-3xl"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -bottom-40 -right-40 h-96 w-96 rounded-full bg-blue-600/20 blur-3xl"
      />

      <div className="relative z-10 w-full max-w-md">
        {/* Logo / brand */}
        <div className="mb-8 text-center">
          <div className="mb-3 inline-flex h-12 w-12 items-center justify-center rounded-xl bg-purple-600 shadow-lg shadow-purple-600/40">
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
          <h1 className="text-3xl font-bold tracking-tight text-white">Smart Order</h1>
          <p className="mt-1 text-sm text-slate-400">Order management &amp; scheduling platform</p>
        </div>

        {/* Glassmorphism card */}
        <Card className="border-white/10 bg-white/5 shadow-2xl backdrop-blur-xl">
          <CardHeader className="space-y-1 pb-4">
            <CardTitle className="text-xl text-white">
              {mode === 'login' ? 'Welcome back' : 'Create your account'}
            </CardTitle>
            <CardDescription className="text-slate-400">
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
                className="mb-4 rounded-md border border-green-500/30 bg-green-500/10 px-4 py-3 text-sm text-green-400"
              >
                Account created! Please sign in.
              </div>
            ) : null}

            {/* Animated form swap */}
            <div key={mode} className="animate-in fade-in duration-300">
              {mode === 'login' ? (
                <LoginForm
                  {...(onLoginSuccess !== undefined ? { onSuccess: onLoginSuccess } : {})}
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

        <p className="mt-6 text-center text-xs text-slate-500">
          Smart Order Management System &copy; {new Date().getFullYear()}
        </p>
      </div>
    </div>
  );
}
