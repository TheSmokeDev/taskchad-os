import {
  LayoutGrid, ListTodo, Users, MessageSquare,
  Brain, Network, Activity, ShieldCheck,
  Briefcase, Mic, Calendar,
  Bot, Kanban, Monitor, Settings as SettingsIcon,
  Smartphone,
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
  { path: '/work',          label: 'Work Queue',      section: 'workspace',    icon: Kanban,        shortcut: 'g q' },
  { path: '/convoy',        label: 'Convoy',          section: 'workspace',    icon: Network,       shortcut: 'g v' },
  { path: '/scheduled',     label: 'Scheduled',       section: 'workspace',    icon: ListTodo,      shortcut: 'g s' },
  { path: '/agents',        label: 'Agents',          section: 'workspace',    icon: Users,         shortcut: 'g a' },
  { path: '/chat',          label: 'Chat',            section: 'workspace',    icon: MessageSquare, shortcut: 'g c' },
  { path: '/browser',       label: 'Browser Viewer',  section: 'workspace',    icon: Monitor,       shortcut: 'g b' },
  { path: '/mobile',        label: 'Mobile Access',   section: 'workspace',    icon: Smartphone,    shortcut: 'g p' },

  { path: '/memories',      label: 'Memories',        section: 'intelligence', icon: Brain,         shortcut: 'g e' },
  { path: '/hive',          label: 'Hive Mind',       section: 'intelligence', icon: Network,       shortcut: 'g h' },
  { path: '/usage',         label: 'Usage',           section: 'intelligence', icon: Activity,      shortcut: 'g u' },
  { path: '/jarvis',        label: 'Jarvis',          section: 'intelligence', icon: Bot,           shortcut: 'g j' },
  { path: '/audit',         label: 'Audit',           section: 'intelligence', icon: ShieldCheck                   },

  { path: '/cabinet',       label: 'Cabinet',         section: 'collaborate',  icon: Briefcase,     shortcut: 'g w' },
  { path: '/teams',         label: 'Teams',           section: 'collaborate',  icon: Users,         shortcut: 'g t' },
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
