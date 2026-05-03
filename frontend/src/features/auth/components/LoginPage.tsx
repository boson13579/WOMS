/**
 * Login page — centered layout that hosts the existing `LoginForm`.
 *
 * Lives outside the `AppShell` so the user isn't shown the sidebar before
 * authenticating. Phase 2 will gate the dashboard behind real auth.
 */
import { LoginForm } from './LoginForm';

export function LoginPage(): JSX.Element {
  return (
    <main className="flex min-h-screen items-center justify-center bg-muted/40 p-4">
      <div className="w-full max-w-md">
        <header className="mb-6 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Smart Order Management</h1>
          <p className="text-sm text-muted-foreground">Phase 1 mock · sign in to continue</p>
        </header>
        <LoginForm />
      </div>
    </main>
  );
}
