import { describe, expect, it } from 'vitest';
import {
  eventFromHistory,
  eventFromStream,
  mergeChatEvent,
  personaMatches,
} from '@/lib/conversation';

describe('native conversation adapter', () => {
  it('normalizes history without trusting malformed content', () => {
    const event = eventFromHistory({ id: 7, role: 'assistant', content: 'hello', timestamp: 42 });
    expect(event).toMatchObject({
      id: 'history-7',
      type: 'assistant_message',
      text: 'hello',
      timestamp: 42,
    });
  });

  it('rejects unknown and malformed stream events', () => {
    expect(eventFromStream('mystery', {})).toBeNull();
    expect(eventFromStream('assistant_message', null)).toBeNull();
  });

  it('keeps only schema-shaped action components', () => {
    const event = eventFromStream('assistant_message', {
      event_id: 12,
      text: 'Choose',
      components: [
        { label: 'Approve once', custom_id: 'approve:12', style: 'primary' },
        { label: 'broken' },
        null,
      ],
    });
    expect(event?.components).toEqual([
      { label: 'Approve once', custom_id: 'approve:12', style: 'primary' },
    ]);
  });

  it('replaces progress in place instead of duplicating it', () => {
    const first = eventFromStream('processing', { event_id: 3, text: 'Starting' });
    const replacement = eventFromStream('progress', {
      event_id: 4,
      replaces_event_id: 3,
      text: 'Halfway',
    });
    expect(first).not.toBeNull();
    expect(replacement).not.toBeNull();
    const merged = mergeChatEvent([first!], replacement!);
    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({ id: '3', type: 'progress', text: 'Halfway' });
  });

  it('honors the main to default translation boundary', () => {
    expect(personaMatches('default', 'main')).toBe(true);
    expect(personaMatches('sales', 'main')).toBe(false);
    expect(personaMatches('sales', 'sales')).toBe(true);
  });
});
