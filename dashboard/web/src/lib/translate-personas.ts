/**
 * Q4 main↔default translation — web-side companion to server-side translate.ts.
 *
 * Phase 5a dashboard-owner GAP 1 fix: SSE event payloads carry persona ids
 * inside JSON `data:` lines. Hono `cabinet.ts` is byte-streaming SSE for
 * thin-proxy preservation, so the SSE outbound translation site MOVES from
 * the canonical Hono boundary to this client lib. This is the SECOND
 * authoritative Q4 site (gated by test) — the only second site allowed.
 *
 *   - `outboundPersonaId('default')` → `'main'`  (framework → browser)
 *   - `outboundPersonaId(...other)`  → identity
 *   - `inboundPersonaId('main')`     → `'default'` (browser → framework)
 *   - `inboundPersonaId(...other)`   → identity
 */

export function outboundPersonaId(personaId: string | undefined | null): string | undefined | null {
  if (personaId === 'default') {
    return 'main';
  }
  return personaId;
}

export function inboundPersonaId(personaId: string | undefined | null): string | undefined | null {
  if (personaId === 'main') {
    return 'default';
  }
  return personaId;
}

/**
 * Translate persona-id-bearing fields on a CabinetEvent payload IN PLACE
 * (returns a NEW object — does not mutate input).
 *
 * Covers every persona-id-bearing field across the 20 CabinetEvent variants
 * per `warroom-text-events.ts:20-45` + dashboard-owner GAP 1 enumeration:
 *   agentId            (status_update, agent_selected, agent_typing,
 *                       agent_chunk, agent_done, intervention_skipped,
 *                       tool_call, tool_result)
 *   pinnedAgent        (meeting_state, meeting_state_update)
 *   primary            (turn_start, router_decision, turn_complete)
 *   speaker            (transcript history rows)
 *   clearedAgents[]    (turn_aborted)
 *   interveners[]      (router_decision)
 *   agents[].id        (meeting_state)
 */
export function translateCabinetEventOutbound(event: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...event };

  // Scalar persona-id fields
  for (const k of ['agentId', 'pinnedAgent', 'primary', 'speaker'] as const) {
    if (typeof out[k] === 'string') {
      out[k] = outboundPersonaId(out[k] as string);
    }
  }

  // Array of persona ids
  for (const arrKey of ['clearedAgents', 'interveners'] as const) {
    if (Array.isArray(out[arrKey])) {
      out[arrKey] = (out[arrKey] as unknown[]).map((id) =>
        typeof id === 'string' ? outboundPersonaId(id) : id,
      );
    }
  }

  // agents[].id (roster)
  if (Array.isArray(out.agents)) {
    out.agents = (out.agents as unknown[]).map((a) => {
      if (a && typeof a === 'object' && 'id' in a && typeof (a as { id: unknown }).id === 'string') {
        return { ...(a as Record<string, unknown>), id: outboundPersonaId((a as { id: string }).id) };
      }
      return a;
    });
  }

  return out;
}
