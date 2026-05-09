/**
 * CabinetTranscript — discriminated render over CabinetEvent variants.
 *
 * PRD-8 Phase 5a / WS4.4. HOMIE-NATIVE — NOT a port of WarRoom.tsx
 * (which only lists meetings + redirects per WarRoom.tsx:291-349). The
 * 20 event variants drive the render switch.
 *
 * Tool-call disclosure UI: tool_call/tool_result events render collapsed
 * by default with click-to-expand affordance.
 */

import { useState } from 'preact/hooks';

interface TranscriptRow {
  id: number;
  speaker: string;
  text: string;
  created_at: number;
}

export interface CabinetEventLike {
  type: string;
  [k: string]: unknown;
}

interface Props {
  baselineRows: TranscriptRow[];
  liveEvents: Array<{ seq: number; event: CabinetEventLike }>;
}

function formatTime(ts: number): string {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleTimeString();
}

function ToolCallDisclosure({ event }: { event: CabinetEventLike }) {
  const [expanded, setExpanded] = useState(false);
  const tool = String(event.tool ?? '');
  const argsPreview = String(event.argsPreview ?? '');
  const agentId = String(event.agentId ?? '');
  return (
    <div class="my-2 px-3 py-2 bg-[var(--color-card)] border border-[var(--color-border)] rounded-md text-xs">
      <button
        type="button"
        class="text-blue-500 underline-offset-2 hover:underline"
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? '▾' : '▸'} {agentId} called {tool}
      </button>
      {expanded && (
        <pre class="mt-2 p-2 bg-[var(--color-input)] rounded text-xs overflow-x-auto">{argsPreview}</pre>
      )}
    </div>
  );
}

function ToolResultDisclosure({ event }: { event: CabinetEventLike }) {
  const [expanded, setExpanded] = useState(false);
  const status = String(event.status ?? '');
  const preview = String(event.resultPreview ?? '');
  const agentId = String(event.agentId ?? '');
  const cls = status === 'error' ? 'text-red-500' : 'text-green-600';
  return (
    <div class="my-2 px-3 py-2 bg-[var(--color-card)] border border-[var(--color-border)] rounded-md text-xs">
      <button
        type="button"
        class={`underline-offset-2 hover:underline ${cls}`}
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? '▾' : '▸'} {agentId} → {status}
      </button>
      {expanded && (
        <pre class="mt-2 p-2 bg-[var(--color-input)] rounded text-xs overflow-x-auto">{preview}</pre>
      )}
    </div>
  );
}

function EventRow({ event }: { event: CabinetEventLike }) {
  switch (event.type) {
    case 'agent_done': {
      const agentId = String(event.agentId ?? '');
      const text = String(event.text ?? '');
      const role = String(event.role ?? '');
      return (
        <div class="my-3 px-3 py-2 bg-[var(--color-card)] rounded-md">
          <div class="text-xs text-[var(--color-text-muted)] mb-1">
            {agentId} {role === 'intervener' ? '(intervener)' : ''}
          </div>
          <div class="whitespace-pre-wrap text-sm">{text}</div>
        </div>
      );
    }
    case 'tool_call':
      return <ToolCallDisclosure event={event} />;
    case 'tool_result':
      return <ToolResultDisclosure event={event} />;
    case 'turn_start': {
      return (
        <div class="my-2 px-3 py-2 bg-blue-500/10 rounded-md text-sm">
          <span class="text-xs text-[var(--color-text-muted)]">you: </span>
          {String(event.userText ?? '')}
        </div>
      );
    }
    case 'system_note':
      return (
        <div class="my-2 px-3 py-1 text-xs italic text-[var(--color-text-muted)]">
          {String(event.text ?? '')}
        </div>
      );
    case 'divider':
      return (
        <div class="my-3 border-t border-dashed border-[var(--color-border)] pt-1 text-xs text-center text-[var(--color-text-muted)]">
          {String(event.text ?? '— divider —')}
        </div>
      );
    case 'meeting_ended':
      return (
        <div class="my-3 text-xs text-center text-red-500">Meeting ended.</div>
      );
    case 'router_decision': {
      const primary = event.primary ? `→ ${String(event.primary)}` : '(silent)';
      return (
        <div class="my-1 text-xs text-[var(--color-text-muted)]">
          router: {primary}
        </div>
      );
    }
    case 'turn_aborted':
      return (
        <div class="my-2 text-xs text-yellow-500 italic">turn aborted</div>
      );
    case 'error':
      return (
        <div class="my-2 px-3 py-2 bg-red-500/10 text-red-500 rounded-md text-sm">
          error: {String(event.message ?? '')}
        </div>
      );
    case 'agent_typing':
      return (
        <div class="my-1 text-xs text-[var(--color-text-muted)] italic">
          {String(event.agentId ?? '')} is typing…
        </div>
      );
    default:
      return null;
  }
}

export function CabinetTranscript({ baselineRows, liveEvents }: Props) {
  return (
    <div class="flex-1 overflow-y-auto px-4 py-2">
      {baselineRows.map((row) => (
        <div key={`row-${row.id}`} class="my-2 px-3 py-2 bg-[var(--color-card)] rounded-md">
          <div class="text-xs text-[var(--color-text-muted)] mb-1">
            {row.speaker} <span class="opacity-50">{formatTime(row.created_at)}</span>
          </div>
          <div class="whitespace-pre-wrap text-sm">{row.text}</div>
        </div>
      ))}
      {liveEvents.map((entry) => (
        <EventRow key={`evt-${entry.seq}`} event={entry.event} />
      ))}
    </div>
  );
}
