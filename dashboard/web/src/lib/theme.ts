import { signal, effect } from '@preact/signals';

export type ThemeName = 'graphite' | 'midnight' | 'crimson';

const STORAGE_KEY = 'homie.theme';
const ACCENT_KEY = 'homie.theme.customAccent';
const SCALE_KEY = 'homie.uiScale';
const SHOW_COSTS_KEY = 'homie.showCosts';

function loadInitial(): ThemeName {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'graphite' || saved === 'midnight' || saved === 'crimson') {
      return saved;
    }
  } catch {}
  return 'graphite';
}

function loadCustomAccent(): string | null {
  try {
    const v = localStorage.getItem(ACCENT_KEY);
    if (v && /^#[0-9a-fA-F]{6}$/.test(v)) return v.toLowerCase();
  } catch {}
  return null;
}

function loadScale(): number {
  try {
    const v = parseFloat(localStorage.getItem(SCALE_KEY) || '');
    if (Number.isFinite(v) && v >= 0.8 && v <= 1.6) return v;
  } catch {}
  return 1.0;
}

function loadShowCosts(): boolean {
  try {
    const v = localStorage.getItem(SHOW_COSTS_KEY);
    if (v === 'on') return true;
    if (v === 'off') return false;
  } catch {}
  // Default OFF — most operators are on Claude Max subscription path
  // where cost-per-token is irrelevant. Toggle on if running on API.
  return false;
}

export const theme = signal<ThemeName>(loadInitial());
export const customAccent = signal<string | null>(loadCustomAccent());
export const uiScale = signal<number>(loadScale());
export const showCosts = signal<boolean>(loadShowCosts());

export const themeMeta: Record<ThemeName, { label: string; swatch: string }> = {
  graphite: { label: 'Graphite', swatch: '#8b8af0' },
  midnight: { label: 'Midnight', swatch: '#5eb6ff' },
  crimson: { label: 'Crimson', swatch: '#ff5e6e' },
};

effect(() => {
  if (typeof document === 'undefined') return;
  const next = theme.value;
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem(STORAGE_KEY, next); } catch {}
});

effect(() => {
  if (typeof document === 'undefined') return;
  const accent = customAccent.value;
  const root = document.documentElement;
  if (accent) {
    root.style.setProperty('--color-accent', accent);
    root.style.setProperty(
      '--color-accent-soft',
      `color-mix(in srgb, ${accent} 18%, transparent)`,
    );
    root.style.setProperty('--color-accent-hover', shadeHex(accent, -10));
    try { localStorage.setItem(ACCENT_KEY, accent); } catch {}
  } else {
    root.style.removeProperty('--color-accent');
    root.style.removeProperty('--color-accent-soft');
    root.style.removeProperty('--color-accent-hover');
    try { localStorage.removeItem(ACCENT_KEY); } catch {}
  }
});

effect(() => {
  if (typeof document === 'undefined') return;
  const s = uiScale.value;
  document.documentElement.style.zoom = String(s);
  try { localStorage.setItem(SCALE_KEY, String(s)); } catch {}
});

effect(() => {
  if (typeof localStorage === 'undefined') return;
  try { localStorage.setItem(SHOW_COSTS_KEY, showCosts.value ? 'on' : 'off'); } catch {}
});

export function setTheme(next: ThemeName) {
  theme.value = next;
}

export function setCustomAccent(hex: string | null) {
  if (hex && !/^#[0-9a-fA-F]{6}$/.test(hex)) return;
  customAccent.value = hex ? hex.toLowerCase() : null;
}

export function setUiScale(next: number) {
  uiScale.value = Math.max(0.8, Math.min(1.6, next));
}

export function setShowCosts(next: boolean) {
  showCosts.value = next;
}

function shadeHex(hex: string, pct: number): string {
  const m = /^#([0-9a-fA-F]{6})$/.exec(hex);
  if (!m) return hex;
  const num = parseInt(m[1], 16);
  let r = (num >> 16) & 0xff;
  let g = (num >> 8) & 0xff;
  let b = num & 0xff;
  const t = pct < 0 ? 0 : 255;
  const p = Math.abs(pct) / 100;
  r = Math.round((t - r) * p + r);
  g = Math.round((t - g) * p + g);
  b = Math.round((t - b) * p + b);
  return '#' + ((r << 16) | (g << 8) | b).toString(16).padStart(6, '0');
}
