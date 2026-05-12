/**
 * Persistent left sidebar — Notion-style nav drawer.
 *
 * Uses NavLink so the active route gets a tonal background highlight and a
 * subtle accent stripe, matching the aesthetic of Vuetify navigation drawers
 * (s-ui style) but rendered with Tailwind primitives.
 *
 * Dashboard is always wired up. User management is shown for root users; the
 * remaining links are visible but disabled to communicate the planned IA.
 *
 * The nav body is split out as `SidebarNavContent` so the mobile drawer
 * (`MobileNav`) can reuse the same markup at viewport widths where the
 * persistent sidebar is hidden (`<768px`).
 */
import {
  Bell,
  CalendarClock,
  LayoutDashboard,
  Package,
  ScrollText,
  Settings,
  Users,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { NavLink } from 'react-router-dom';

import { Separator } from '@/components/ui/separator';
import { useCurrentRole } from '@/lib/auth';
import { cn } from '@/lib/utils';

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  disabled?: boolean;
}

const PRIMARY_NAV: readonly NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/orders', label: 'Orders', icon: Package },
  { to: '/scheduling', label: 'Scheduling', icon: CalendarClock, disabled: true },
];

const SECONDARY_NAV: readonly NavItem[] = [
  { to: '/audit', label: 'Audit log', icon: ScrollText, disabled: true },
  { to: '/notifications', label: 'Notifications', icon: Bell, disabled: true },
];

function NavRow({ item, onNavigate }: { item: NavItem; onNavigate: () => void }): JSX.Element {
  const { to, label, icon: Icon, disabled } = item;

  if (disabled) {
    return (
      <div
        className="flex cursor-not-allowed items-center gap-3 rounded-md px-3 py-2 text-sm text-muted-foreground/60"
        title="Coming in Phase 2"
      >
        <Icon className="h-4 w-4" />
        <span className="flex-1">{label}</span>
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          soon
        </span>
      </div>
    );
  }

  return (
    <NavLink
      to={to}
      end
      onClick={onNavigate}
      className={({ isActive }) =>
        cn(
          'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
          isActive
            ? 'bg-secondary text-secondary-foreground'
            : 'text-muted-foreground hover:bg-secondary/60 hover:text-foreground',
        )
      }
    >
      <Icon className="h-4 w-4" />
      <span>{label}</span>
    </NavLink>
  );
}

interface SidebarNavContentProps {
  /**
   * Fired when the user activates a nav link. The mobile drawer wires
   * this to close-on-navigate so a tap on "Orders" both routes AND
   * dismisses the drawer.
   */
  onNavigate?: () => void;
}

const NOOP = (): void => {};

export function SidebarNavContent({ onNavigate = NOOP }: SidebarNavContentProps = {}): JSX.Element {
  const role = useCurrentRole();
  const showUserManagement = role === 'root';

  return (
    <div className="flex h-full flex-col">
      {/* Brand */}
      <div className="flex h-14 items-center gap-2 px-5">
        <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
          <Package className="h-4 w-4" />
        </div>
        <div className="leading-tight">
          <p className="text-sm font-semibold">WOMS</p>
          <p className="text-[10px] uppercase tracking-wider text-muted-foreground">Smart Orders</p>
        </div>
      </div>

      <Separator />

      {/* Nav */}
      <nav className="flex flex-1 flex-col gap-6 p-3">
        <div>
          <p className="px-3 pb-1.5 pt-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Workspace
          </p>
          <div className="flex flex-col gap-0.5">
            {PRIMARY_NAV.map((item) => (
              <NavRow key={item.to} item={item} onNavigate={onNavigate} />
            ))}
            {showUserManagement ? (
              <NavRow
                item={{ to: '/users', label: 'Users', icon: Users }}
                onNavigate={onNavigate}
              />
            ) : null}
          </div>
        </div>

        <div>
          <p className="px-3 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Activity
          </p>
          <div className="flex flex-col gap-0.5">
            {SECONDARY_NAV.map((item) => (
              <NavRow key={item.to} item={item} onNavigate={onNavigate} />
            ))}
          </div>
        </div>
      </nav>

      {/* Footer */}
      <div className="p-3">
        <Separator className="mb-3" />
        <div className="flex cursor-not-allowed items-center gap-3 rounded-md px-3 py-2 text-sm text-muted-foreground/60">
          <Settings className="h-4 w-4" />
          <span className="flex-1">Settings</span>
        </div>
        <div className="mt-2 px-3 text-[10px] text-muted-foreground">Phase 1 · v0.1.0</div>
      </div>
    </div>
  );
}

export function Sidebar(): JSX.Element {
  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-card md:flex">
      <SidebarNavContent />
    </aside>
  );
}
