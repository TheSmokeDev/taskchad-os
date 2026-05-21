import { describe, test, expect } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { BrainGraph } from '@/components/BrainGraph';
import { BrainGraph3D, getBrain3DRenderBudget, getBrainNodeDetailBody } from '@/components/BrainGraph3D';

const entries = [
  {
    id: 1,
    agent_id: 'main',
    chat_id: 'session-a',
    action: 'chat_message:assistant',
    summary: 'Recent hive activity',
    artifacts: null,
    created_at: Date.now() / 1000,
  },
  {
    id: 2,
    agent_id: 'research',
    chat_id: 'session-a',
    action: 'chat_message:user',
    summary: 'Research handoff',
    artifacts: 'claude / opus',
    created_at: Date.now() / 1000 - 30,
  },
];

const graphData = {
  nodes: [
    {
      id: 'chunk:1',
      label: 'Durable memory node',
      kind: 'chunk',
      scope_type: 'global',
      scope_id: 'main',
      text: 'Shared memory topology appears in 3D fallback.',
    },
    {
      id: 'note:memory',
      label: 'Memory note',
      kind: 'note',
      scope_type: 'global',
      scope_id: 'main',
    },
  ],
  edges: [
    { id: 'edge:1', source: 'chunk:1', target: 'note:memory', kind: 'source' },
  ],
  activity: entries,
  stats: { total_nodes: 2, total_edges: 1, activity_count: 2 },
};

describe('BrainGraph3D', () => {
  test('keeps Phase 4 3D budgets capped while adding quality and LOD controls', () => {
    expect(getBrain3DRenderBudget('high', 'detail')).toEqual({
      nodes: 260,
      edges: 420,
      activity: 180,
    });
    expect(getBrain3DRenderBudget('medium', 'balanced').nodes).toBeLessThan(260);
    expect(getBrain3DRenderBudget('low', 'clusters').edges).toBeLessThan(420);
    expect(getBrain3DRenderBudget('high', 'clusters').activity).toBeLessThan(180);
  });

  test('uses real node text as the 3D memory detail body before metadata fallbacks', () => {
    expect(getBrainNodeDetailBody({
      id: 'note:memory',
      label: 'Memory note',
      kind: 'note',
      scope_type: 'global',
      scope_id: 'main',
      text: 'Actual loaded note body.',
      section_title: 'Fallback section',
    })).toBe('Actual loaded note body.');
    expect(getBrainNodeDetailBody({
      id: 'note:empty',
      label: 'Empty note',
      kind: 'note',
      scope_type: 'global',
      scope_id: 'main',
      section_title: 'Only section available',
    })).toBe('Only section available');
  });

  test('derives 3D note detail bodies from loaded chunk neighbors', () => {
    expect(getBrainNodeDetailBody({
      id: 'note:memory',
      label: 'Memory note',
      kind: 'note',
      scope_type: 'global',
      scope_id: 'main',
    }, [{
      id: 'chunk:1',
      label: 'Chunk one',
      kind: 'chunk',
      scope_type: 'global',
      scope_id: 'main',
      section_title: 'Chunk Section',
      text: 'Actual loaded chunk body.',
      created_at: 1760000000,
    }])).toContain('Actual loaded chunk body.');
  });

  test('explains when a 3D note aggregate has no loaded body in the current page', () => {
    expect(getBrainNodeDetailBody({
      id: 'note:external',
      label: 'External note',
      kind: 'note',
      scope_type: 'global',
      scope_id: 'main',
      source_path: 'AGENT-ARCHITECTURE.md',
    })).toContain('Loaded body is not available in the current graph page');
  });

  test('falls back to the 2D ClaudeClaw brain when WebGL is unavailable', () => {
    const { container } = render(
      <BrainGraph3D
        entries={entries}
        agentFilter="all"
        agentColors={{ main: '#8b8af0', research: '#5eb6ff' }}
        blurOn={false}
      />,
    );

    expect(container.querySelector('svg')).toBeTruthy();
    expect(screen.getByText('Frontal')).toBeInTheDocument();
    expect(screen.getAllByText('Filters').length).toBeGreaterThan(0);
  });

  test('falls back to the shared brain graph topology when graph data is supplied', () => {
    render(
      <BrainGraph3D
        data={graphData}
        entries={entries}
        agentFilter="all"
        agentColors={{ main: '#8b8af0', research: '#5eb6ff' }}
        blurOn={false}
      />,
    );

    expect(screen.getByLabelText('Homie brain graph')).toBeInTheDocument();
    expect(screen.getByText('Shared memory topology appears in 3D fallback.')).toBeInTheDocument();
    expect(screen.getAllByText('Durable memory node').length).toBeGreaterThan(0);
  });
});

describe('BrainGraph', () => {
  test('opens the ClaudeClaw detail panel from a brain node', async () => {
    const { container } = render(
      <BrainGraph
        entries={entries}
        agentFilter="all"
        agentColors={{ main: '#8b8af0', research: '#5eb6ff' }}
        blurOn={false}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector('g.brain-dot-bloom')).toBeTruthy();
    });

    fireEvent.click(container.querySelector('g.brain-dot-bloom') as Element);

    await waitFor(() => expect(screen.getByText('chat_message:assistant')).toBeInTheDocument());
    expect(screen.getByText('Recent hive activity')).toBeInTheDocument();
  });
});
