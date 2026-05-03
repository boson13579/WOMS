/**
 * Two-pane application shell: persistent left sidebar + scrollable main area.
 *
 * Pages render via `<Outlet />` so each route owns its own header and content
 * (the dashboard, for example, supplies a `Header` with a refresh button while
 * a future "Settings" page might use no header at all).
 */
import { Outlet } from 'react-router-dom';

import { Sidebar } from '@/components/layout/Sidebar';

export function AppShell(): JSX.Element {
  return (
    <div className="flex min-h-screen bg-muted/30">
      <Sidebar />
      <main className="flex min-h-screen flex-1 flex-col">
        <Outlet />
      </main>
    </div>
  );
}
