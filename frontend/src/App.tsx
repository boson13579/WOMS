/**
 * Application shell.
 *
 * Phase 1 placeholder: shows the auth feature's LoginForm so a fresh clone
 * can verify the entire toolchain (Vite + Tailwind + shadcn tokens + React
 * Query + Bulletproof folder structure) at a glance.
 *
 * Phase 2 will replace this with `<RouterProvider>` from react-router-dom.
 */
import { LoginForm } from '@/features/auth/components/LoginForm';

function App(): JSX.Element {
  return (
    <main className="min-h-screen flex items-center justify-center bg-background p-4">
      <div className="w-full max-w-md">
        <header className="mb-6 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Smart Order Management</h1>
          <p className="text-sm text-muted-foreground">Phase 1 — scaffolding only</p>
        </header>
        <LoginForm />
      </div>
    </main>
  );
}

export default App;
