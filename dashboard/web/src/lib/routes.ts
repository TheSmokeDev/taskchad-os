import {
  LayoutGrid, ListTodo, Users, MessageSquare,
  Brain, Network, Activity, ShieldCheck,
  Briefcase, Mic, Calendar,
  Settings as SettingsIcon,
} from 'lucide-preact';
import type { ComponentChildren } from 'preact';

export type RouteSection = 'workspace' | 'intelligence' | 'collaborate' | 'configure';

export interface RouteDef {
  path: string;
  label: string;
  section: RouteSection;
  icon: typeof LayoutGrid;
  shortcut?: string;
}

// Single source of truth for the sidebar, command palette, and router.
// Renames vs donor (per INTENTIONAL_DEVIATIONS.md):
//   - WarRoom → Cabinet (Q-naming lock; route /cabinet)
export const ROUTES: RouteDef[] = [
  { path: '/mission',       label: 'Mission Control', section: 'workspace',    icon: LayoutGrid,    shortcut: 'g m' },
  { path: '/scheduled',     label: 'Scheduled',       section: 'workspace',    icon: ListTodo,      shortcut: 'g s' },
  { path: '/agents',        label: 'Agents',          section: 'workspace',    icon: Users,         shortcut: 'g a' },
  { path: '/chat',          label: 'Chat',            section: 'workspace',    icon: MessageSquare, shortcut: 'g c' },

  { path: '/memories',      label: 'Memories',        section: 'intelligence', icon: Brain,         shortcut: 'g e' },
  { path: '/hive',          label: 'Hive Mind',       section: 'intelligence', icon: Network,       shortcut: 'g h' },
  { path: '/usage',         label: 'Usage',           section: 'intelligence', icon: Activity,      shortcut: 'g u' },
  { path: '/audit',         label: 'Audit',           section: 'intelligence', icon: ShieldCheck                   },

  { path: '/cabinet',       label: 'Cabinet',         section: 'collaborate',  icon: Briefcase,     shortcut: 'g w' },
  { path: '/voices',        label: 'Voices',          section: 'collaborate',  icon: Mic                           },
  { path: '/standup',       label: 'Standup',         section: 'collaborate',  icon: Calendar                      },

  { path: '/settings',      label: 'Settings',        section: 'configure',    icon: SettingsIcon                  },
];

export const SECTION_LABEL: Record<RouteSection, string> = {
  workspace:    'Workspace',
  intelligence: 'Intelligence',
  collaborate:  'Collaborate',
  configure:    'Configure',
};

export const DEFAULT_ROUTE = '/mission';

/** UI-side default persona id. Browser routing uses 'main' — Hono's
 *  translate.ts translates to 'default' before forwarding to Python.
 *  Q4 lock: this is the ONLY place 'main' is the operator label. */
export const DEFAULT_PERSONA_ID_UI = 'main';

export type PageProps = { children?: ComponentChildren };
