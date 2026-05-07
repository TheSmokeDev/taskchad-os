import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/preact';
import { BrainGraph3D } from '@/components/BrainGraph3D';

describe('BrainGraph3D', () => {
  beforeEach(() => {
    // happy-dom doesn't provide WebGL — the component should fall back
    // to BrainGraph (2D list).
    vi.spyOn(globalThis, 'fetch' as any).mockResolvedValue(
      new Response(JSON.stringify({
        events: [
          { id: 'e1', personaId: 'main', type: 'recall', timestamp: Date.now() / 1000 },
        ],
      }), { status: 200, headers: { 'content-type': 'application/json' } }),
    );
  });

  test('mounts and pulls from /api/hive-mind/recent', async () => {
    const { container } = render(<BrainGraph3D limit={50} />);
    await waitFor(() => {
      // Either the canvas mounts (WebGL path) or the 2D fallback list
      // renders the event — both are valid outcomes given no WebGL in
      // happy-dom. Assert the fetch happened with the right URL.
      expect(globalThis.fetch).toHaveBeenCalled();
      const calls = (globalThis.fetch as any).mock.calls as any[];
      const hiveCall = calls.find(([u]: [string]) => typeof u === 'string' && u.includes('/api/hive-mind/recent'));
      expect(hiveCall).toBeDefined();
    });
    // At least the container exists.
    expect(container.firstChild).toBeTruthy();
  });
});
