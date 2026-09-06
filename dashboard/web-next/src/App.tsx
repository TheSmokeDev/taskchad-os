import { IconDeviceDesktop, IconLayoutSidebarLeftCollapse } from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import { ComputerPanel } from '@/components/ComputerPanel';
import { ConversationPane } from '@/components/ConversationPane';
import { CoworkerSidebar } from '@/components/CoworkerSidebar';
import { useBrowserPreview } from '@/hooks/useBrowserPreview';
import { useConversation } from '@/hooks/useConversation';
import { useCoworkers } from '@/hooks/useCoworkers';
import { coworkerDisplayName, type Coworker } from '@/types';

function initialPersonaId(): string | null {
  const url = new URL(window.location.href);
  return url.searchParams.get('persona');
}

export function App() {
  // Deterministic no-write visual capture: loads physical API state once but
  // does not keep SSE/polling handles open. It changes no authority or data.
  const visualCapture = new URL(window.location.href).searchParams.get('visual') === '1';
  const { coworkers, loading, error } = useCoworkers();
  const [selectedId, setSelectedId] = useState<string | null>(initialPersonaId);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [computerOpen, setComputerOpen] = useState(false);
  const selected = useMemo(
    () => coworkers.find((coworker) => coworker.id === selectedId) ?? coworkers.find((coworker) => coworker.id === 'main') ?? coworkers[0] ?? null,
    [coworkers, selectedId],
  );
  const conversation = useConversation(selected?.id ?? null, !visualCapture);
  const computer = useBrowserPreview(!visualCapture);

  useEffect(() => {
    if (!selected || selected.id === selectedId) return;
    setSelectedId(selected.id);
  }, [selected, selectedId]);

  const selectCoworker = (coworker: Coworker) => {
    setSelectedId(coworker.id);
    const url = new URL(window.location.href);
    url.searchParams.set('persona', coworker.id);
    history.replaceState({}, '', url);
    setSidebarOpen(false);
  };

  if (!loading && error && coworkers.length === 0) {
    return (
      <main className="fatal-state">
        <span>Local Dashboard API unavailable</span>
        <h1>The React foundation could not load Homie personas.</h1>
        <p>{error}</p>
      </main>
    );
  }

  return (
    <div className="app-shell">
      <div className={`mobile-scrim ${sidebarOpen || computerOpen ? 'is-visible' : ''}`} onClick={() => { setSidebarOpen(false); setComputerOpen(false); }} />
      <div className={`sidebar-slot ${sidebarOpen ? 'is-mobile-open' : ''}`}>
        <CoworkerSidebar
          coworkers={coworkers}
          selectedId={selected?.id ?? null}
          onSelect={selectCoworker}
          loading={loading}
        />
      </div>

      <div className="mobile-toolbar">
        <button type="button" onClick={() => setSidebarOpen(true)} aria-label="Open coworker list">
          <IconLayoutSidebarLeftCollapse size={19} />
        </button>
        <strong>{selected ? coworkerDisplayName(selected) : 'The Homie'}</strong>
        <button type="button" onClick={() => setComputerOpen(true)} aria-label="Open computer panel">
          <IconDeviceDesktop size={19} />
        </button>
      </div>

      {selected ? (
        <ConversationPane
          coworker={selected}
          events={conversation.events}
          connected={conversation.connected}
          sending={conversation.sending}
          error={conversation.error}
          onSend={conversation.submit}
          onRefresh={() => conversation.loadHistory()}
        />
      ) : (
        <main className="loading-state">Loading native coworkers…</main>
      )}

      {selected ? (
        <div className={`computer-slot ${computerOpen ? 'is-mobile-open' : ''}`}>
          <ComputerPanel
            coworker={selected}
            status={computer.status}
            frameUrl={computer.frameUrl}
            error={computer.error}
          />
        </div>
      ) : null}
    </div>
  );
}
