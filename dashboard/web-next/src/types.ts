export interface Coworker {
  id: string;
  name: string;
  description: string;
  model: string;
  running: boolean;
  todayTurns?: number;
  todayCost?: number;
  lane?: 'claude_native' | 'generic';
  planQuotaPct?: number;
}

export function coworkerDisplayName(coworker: Coworker): string {
  const supplied = coworker.name?.trim();
  if (
    coworker.id === 'main' &&
    (!supplied || supplied.toLowerCase() === 'default' || supplied.toLowerCase() === 'main')
  ) {
    return 'Homie';
  }
  return supplied || coworker.id;
}

export interface ActionComponent {
  label: string;
  custom_id: string;
  style?: string;
  disabled?: boolean;
}

export type ChatEventType =
  | 'user_message'
  | 'assistant_message'
  | 'processing'
  | 'progress'
  | 'error';

export interface ChatEvent {
  id: string;
  type: ChatEventType;
  text: string;
  timestamp: number;
  components: ActionComponent[];
  replacesEventId?: string;
}

export interface HistoryTurn {
  id: number;
  role: string;
  content: string;
  timestamp?: number;
  created_at?: string;
}

export interface BrowserViewerStatus {
  mode: 'read_only';
  target?: string;
  readiness: {
    status: string;
    cdp_port: number | null;
    cdp_reachable: boolean;
    browser: string;
    visible_guard: string;
    tab_count: number;
    reason: string;
  };
  stream: {
    enabled: boolean;
    connected: boolean;
    port: number | null;
    screencasting: boolean;
    reason?: string;
    direct_ws_url?: string;
  };
  controls: {
    browser_input: false;
    navigation: false;
  };
}
