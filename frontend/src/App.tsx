/**
 * Application shell — Phase 1 Auth.
 *
 * Renders the AuthPage which handles both login and registration.
 * Phase 2 will replace this with <RouterProvider router={router} /> from
 * react-router-dom for full client-side navigation.
 */
import { AuthPage } from '@/features/auth/components/AuthPage';

function App(): JSX.Element {
  return <AuthPage />;
}

export default App;
