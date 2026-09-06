import {
  IconAdjustments,
  IconBriefcase,
  IconMessageCirclePlus,
  IconSearch,
  IconShieldCheck,
  IconUsers,
} from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { AbstractAvatar } from '@/components/AbstractAvatar';
import { coworkerDisplayName, type Coworker } from '@/types';

interface Props {
  coworkers: Coworker[];
  selectedId: string | null;
  onSelect: (coworker: Coworker) => void;
  loading: boolean;
}

export function CoworkerSidebar({ coworkers, selectedId, onSelect, loading }: Props) {
  const [query, setQuery] = useState('');
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return coworkers;
    return coworkers.filter((coworker) =>
      [coworkerDisplayName(coworker), coworker.id, coworker.description].some((value) =>
        value?.toLowerCase().includes(needle),
      ),
    );
  }, [coworkers, query]);

  return (
    <aside className="coworker-sidebar">
      <div className="brand-row">
        <div>
          <div className="brand-name">The Homie</div>
          <div className="brand-kicker">native coworkers</div>
        </div>
        <button className="icon-button" type="button" aria-label="Start a coworker channel">
          <IconMessageCirclePlus size={18} />
        </button>
      </div>

      <label className="search-field">
        <IconSearch size={15} />
        <input
          aria-label="Search coworkers"
          placeholder="Search coworkers..."
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>

      <nav className="primary-nav" aria-label="Workspace">
        <a href="http://127.0.0.1:5173/mission">
          <IconBriefcase size={16} /> Mission
        </a>
        <a href="http://127.0.0.1:5173/teams">
          <IconUsers size={16} /> Teams
        </a>
        <a href="http://127.0.0.1:5173/capabilities">
          <IconAdjustments size={16} /> Capabilities
        </a>
      </nav>

      <div className="section-label">
        <span>Coworkers</span>
        <span>{coworkers.length}</span>
      </div>

      <div className="coworker-list">
        {loading ? <div className="sidebar-note">Loading local personas…</div> : null}
        {!loading && visible.length === 0 ? (
          <div className="sidebar-note">No coworkers match “{query.trim()}”.</div>
        ) : null}
        {visible.map((coworker) => (
          <button
            key={coworker.id}
            type="button"
            className={`coworker-row ${selectedId === coworker.id ? 'is-selected' : ''}`}
            onClick={() => onSelect(coworker)}
          >
            <AbstractAvatar name={coworkerDisplayName(coworker)} seed={coworker.id} size={34} />
            <span className="coworker-copy">
              <span className="coworker-title-row">
                <strong>{coworkerDisplayName(coworker)}</strong>
                <span className={`presence ${coworker.running ? 'is-live' : ''}`} />
              </span>
              <span>{coworker.description || `${coworker.lane ?? 'runtime'} coworker`}</span>
            </span>
          </button>
        ))}
      </div>

      <div className="sidebar-footer">
        <IconShieldCheck size={16} />
        <span>
          Local authority
          <small>Python-owned · fail-closed</small>
        </span>
      </div>
    </aside>
  );
}
