// Tiny WebGL availability probe. Lazy-evaluated per-call so test envs
// (happy-dom) don't crash on canvas access. We do NOT cache at module
// scope — Rule 2 (the result depends on whether window/document is
// available, which differs in test vs runtime).

export function hasWebGL(): boolean {
  if (typeof document === 'undefined') return false;
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl2')
      || c.getContext('webgl')
      || (c.getContext as any)('experimental-webgl');
    return !!gl;
  } catch {
    return false;
  }
}
