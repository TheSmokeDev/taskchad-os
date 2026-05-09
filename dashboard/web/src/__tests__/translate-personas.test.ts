import { describe, test, expect } from 'vitest';
import {
  inboundPersonaId,
  outboundPersonaId,
  translateCabinetEventOutbound,
} from '../lib/translate-personas';

describe('Q4 main↔default translation primitives', () => {
  test('outboundPersonaId maps default → main', () => {
    expect(outboundPersonaId('default')).toBe('main');
  });

  test('inboundPersonaId maps main → default', () => {
    expect(inboundPersonaId('main')).toBe('default');
  });

  test('non-canonical ids pass through identity (both directions)', () => {
    expect(outboundPersonaId('seo')).toBe('seo');
    expect(outboundPersonaId('ops')).toBe('ops');
    expect(outboundPersonaId(undefined)).toBe(undefined);
    expect(outboundPersonaId(null)).toBe(null);
    expect(inboundPersonaId('seo')).toBe('seo');
    expect(inboundPersonaId(undefined)).toBe(undefined);
    expect(inboundPersonaId(null)).toBe(null);
  });
});

describe('translateCabinetEventOutbound — every persona-id-bearing CabinetEvent field', () => {
  test('agentId scalar is translated', () => {
    const out = translateCabinetEventOutbound({ type: 'status_update', agentId: 'default' });
    expect(out.agentId).toBe('main');
  });

  test('pinnedAgent scalar is translated', () => {
    const out = translateCabinetEventOutbound({ type: 'meeting_state', pinnedAgent: 'default' });
    expect(out.pinnedAgent).toBe('main');
  });

  test('primary scalar is translated', () => {
    const out = translateCabinetEventOutbound({ type: 'turn_start', primary: 'default' });
    expect(out.primary).toBe('main');
  });

  test('speaker scalar is translated', () => {
    const out = translateCabinetEventOutbound({ speaker: 'default', text: 'hi' });
    expect(out.speaker).toBe('main');
  });

  test('clearedAgents[] is translated element-wise', () => {
    const out = translateCabinetEventOutbound({
      type: 'turn_aborted',
      clearedAgents: ['default', 'seo'],
    });
    expect(out.clearedAgents).toEqual(['main', 'seo']);
  });

  // Regression test for dashboard-owner GAP — pre-fix, interveners[] was NOT
  // covered. Post-Q4-alignment, the array carries 'default' for the main
  // agent and was reaching the UI without translation.
  test('interveners[] is translated element-wise (regression for dashboard-owner GAP)', () => {
    const out = translateCabinetEventOutbound({
      type: 'router_decision',
      primary: 'default',
      interveners: ['default', 'ops'],
    });
    expect(out.primary).toBe('main');
    expect(out.interveners).toEqual(['main', 'ops']);
  });

  test('agents[].id is translated for meeting_state roster', () => {
    const out = translateCabinetEventOutbound({
      type: 'meeting_state',
      agents: [
        { id: 'default', name: 'Main' },
        { id: 'seo', name: 'SEO' },
      ],
    });
    expect(out.agents).toEqual([
      { id: 'main', name: 'Main' },
      { id: 'seo', name: 'SEO' },
    ]);
  });

  test('returns a NEW object — does not mutate input', () => {
    const input = { type: 'status_update', agentId: 'default' };
    const out = translateCabinetEventOutbound(input);
    expect(input.agentId).toBe('default');
    expect(out.agentId).toBe('main');
    expect(out).not.toBe(input);
  });

  test('non-string scalar fields are passed through (no coercion)', () => {
    const out = translateCabinetEventOutbound({
      type: 'agent_chunk',
      agentId: 'default',
      delta: 'hello',
      seq: 42,
    });
    expect(out.delta).toBe('hello');
    expect(out.seq).toBe(42);
  });

  test('events without persona-id fields are returned unchanged (shallow clone)', () => {
    const out = translateCabinetEventOutbound({ type: 'ping' });
    expect(out).toEqual({ type: 'ping' });
  });
});
