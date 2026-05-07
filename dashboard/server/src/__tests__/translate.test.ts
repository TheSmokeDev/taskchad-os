/**
 * translate.test.ts — Q4 lock single-site behavior verification.
 */

import { describe, expect, it } from 'vitest';
import {
  inboundPersonaId,
  outboundPersonaId,
  outboundPersonaDict,
  outboundPersonaList,
} from '../translate.js';

describe('translate', () => {
  it('inbound: main → default', () => {
    expect(inboundPersonaId('main')).toBe('default');
  });

  it('outbound: default → main', () => {
    expect(outboundPersonaId('default')).toBe('main');
  });

  it('identity for other ids', () => {
    expect(inboundPersonaId('sales-homie')).toBe('sales-homie');
    expect(outboundPersonaId('sales-homie')).toBe('sales-homie');
    expect(inboundPersonaId('default')).toBe('default'); // inbound never maps default→anything
    expect(outboundPersonaId('main')).toBe('main'); // outbound never maps main→anything
  });

  it('identity for empty/undefined', () => {
    expect(inboundPersonaId('')).toBe('');
    expect(inboundPersonaId(undefined)).toBe(undefined);
    expect(inboundPersonaId(null)).toBe(null);
    expect(outboundPersonaId('')).toBe('');
    expect(outboundPersonaId(undefined)).toBe(undefined);
    expect(outboundPersonaId(null)).toBe(null);
  });

  it('outboundPersonaDict rewrites id default → main', () => {
    expect(outboundPersonaDict({ id: 'default', name: 'owner' })).toEqual({
      id: 'main',
      name: 'owner',
    });
    expect(outboundPersonaDict({ id: 'sales-homie', name: 'owner' })).toEqual({
      id: 'sales-homie',
      name: 'owner',
    });
  });

  it('outboundPersonaList maps over array', () => {
    const input = [
      { id: 'default', name: 'A' },
      { id: 'sales-homie', name: 'B' },
    ];
    const out = outboundPersonaList(input);
    expect(out).toEqual([
      { id: 'main', name: 'A' },
      { id: 'sales-homie', name: 'B' },
    ]);
  });
});
