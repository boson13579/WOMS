/**
 * App-level providers.
 *
 * `main.tsx` mounts this; the router (and therefore route components) live
 * inside `<RouterProvider>` so future code can read `useNavigate` etc.
 */
import { RouterProvider } from 'react-router-dom';
import { Toaster } from 'sonner';

import { router } from '@/routes/router';

function App(): JSX.Element {
  return (
    <>
      <RouterProvider router={router} />
      <Toaster richColors position="top-right" />
    </>
  );
}

export default App;
