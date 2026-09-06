import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => cleanup());

Object.defineProperty(URL, 'createObjectURL', {
  configurable: true,
  value: () => 'blob:homie-browser-frame',
});

Object.defineProperty(URL, 'revokeObjectURL', {
  configurable: true,
  value: () => undefined,
});

class FakeEventSource {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  readonly url: string;
  private listeners = new Map<string, Set<EventListenerOrEventListenerObject>>();

  constructor(url: string | URL) {
    this.url = String(url);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject | null) {
    if (!listener) return;
    const current = this.listeners.get(type) ?? new Set<EventListenerOrEventListenerObject>();
    current.add(listener);
    this.listeners.set(type, current);
  }

  removeEventListener(type: string, listener: EventListenerOrEventListenerObject | null) {
    if (!listener) return;
    this.listeners.get(type)?.delete(listener);
  }

  close() {
    this.listeners.clear();
  }
}

Object.defineProperty(globalThis, 'EventSource', {
  configurable: true,
  value: FakeEventSource,
});
