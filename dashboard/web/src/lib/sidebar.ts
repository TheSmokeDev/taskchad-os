import { signal } from '@preact/signals';

/** Mobile sidebar open state. Desktop ignores this — the sidebar is
 *  always visible above the `md:` breakpoint via Tailwind classes. */
export const sidebarOpen = signal(false);

export function closeSidebar(): void {
  sidebarOpen.value = false;
}
