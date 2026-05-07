/**
 * Persona id translation — the SINGLE source of main↔default mapping.
 *
 * Q4 lock (PRP-prd-8-phase-3, dashboard-owner charter):
 * - Browser/frontend speaks `main` for the default persona (ClaudeClaw donor convention).
 * - Python framework canonical id is `default`.
 * - Translation happens at the Hono boundary — ONE site only — this file.
 *
 * Every Hono route handler MUST:
 *   1. Call `inboundPersonaId(...)` BEFORE forwarding to port 4322.
 *   2. Call `outboundPersonaId(...)` BEFORE returning to the browser.
 *
 * Python framework rejects `persona_id='main'` with HTTP 422 — Hono is the
 * only translation site. A duplicate translation anywhere else is a Q4
 * lock violation.
 *
 * The functions are intentionally pure and trivial:
 * - `inboundPersonaId('main')` → `'default'`
 * - `outboundPersonaId('default')` → `'main'`
 * - Identity for any other id (including empty/undefined).
 */

/**
 * Translate browser-facing persona id to Python framework canonical id.
 *
 * Maps `main` → `default`. Identity for any other id, including empty
 * string and undefined (returned as-is, callers handle missing ids).
 */
export function inboundPersonaId(personaId: string | undefined | null): string | undefined | null {
  if (personaId === 'main') {
    return 'default';
  }
  return personaId;
}

/**
 * Translate Python framework canonical id to browser-facing persona id.
 *
 * Maps `default` → `main`. Identity for any other id.
 */
export function outboundPersonaId(personaId: string | undefined | null): string | undefined | null {
  if (personaId === 'default') {
    return 'main';
  }
  return personaId;
}

/**
 * Outbound translate a full persona dict — rewrite `id` field from
 * `default` to `main` if present. Other fields untouched. Used by route
 * handlers to translate response bodies before returning to the browser.
 */
export function outboundPersonaDict<T extends Record<string, unknown>>(dict: T): T {
  if (dict && typeof dict === 'object' && 'id' in dict && dict.id === 'default') {
    return { ...dict, id: 'main' };
  }
  // Also handle persona_id field (used in some responses).
  if (dict && typeof dict === 'object' && 'persona_id' in dict && dict.persona_id === 'default') {
    return { ...dict, persona_id: 'main' };
  }
  return dict;
}

/**
 * Outbound translate a list of persona dicts.
 */
export function outboundPersonaList<T extends Record<string, unknown>>(list: T[]): T[] {
  return list.map((d) => outboundPersonaDict(d));
}
