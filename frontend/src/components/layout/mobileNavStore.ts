/**
 * Cross-component open/close state for the mobile navigation drawer.
 *
 * The hamburger trigger lives in `Header` (rendered by each page), while
 * the drawer itself is mounted once in `AppShell`. Lifting via props
 * would require threading state through every page component, so a tiny
 * store wins on plumbing cost.
 */
import { create } from 'zustand';

interface MobileNavState {
  open: boolean;
  setOpen: (open: boolean) => void;
}

export const useMobileNavStore = create<MobileNavState>((set) => ({
  open: false,
  setOpen: (open) => {
    set({ open });
  },
}));
