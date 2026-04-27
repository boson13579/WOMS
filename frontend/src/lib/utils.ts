/**
 * `cn(...inputs)` — merge Tailwind class names with conflict resolution.
 *
 * `clsx` handles conditional class composition; `twMerge` resolves duplicate
 * Tailwind utilities (so `cn('px-2', 'px-4')` correctly yields `'px-4'`).
 *
 * Required by every shadcn/ui component, so it lives at a stable path.
 */
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
