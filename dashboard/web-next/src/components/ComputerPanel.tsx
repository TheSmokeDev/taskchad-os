import {
  IconActivity,
  IconBrandChrome,
  IconDeviceDesktop,
  IconLock,
  IconShieldCheck,
} from '@tabler/icons-react';
import { AbstractAvatar } from '@/components/AbstractAvatar';
import { coworkerDisplayName, type BrowserViewerStatus, type Coworker } from '@/types';

interface Props {
  coworker: Coworker;
  status: BrowserViewerStatus | null;
  frameUrl: string | null;
  error: string | null;
}

export function ComputerPanel({ coworker, status, frameUrl, error }: Props) {
  const displayName = coworkerDisplayName(coworker);
  const ready = status?.readiness.cdp_reachable === true;
  return (
    <aside className="computer-panel">
      <header className="panel-header">
        <div>
          <strong>Computer</strong>
          <span>{displayName}</span>
        </div>
        <span className="readonly-pill">
          <IconShieldCheck size={13} /> read only
        </span>
      </header>

      <div className="computer-frame">
        {frameUrl ? <img src={frameUrl} alt="Current visible Homie browser" /> : null}
        {!frameUrl ? (
          <div className="computer-placeholder">
            <div className="placeholder-glow" />
            <IconDeviceDesktop size={38} />
            <strong>{error ? 'Screen unavailable' : 'Waiting for local screen'}</strong>
            <span>{error || status?.readiness.reason || 'Browser Viewer has not returned a frame yet.'}</span>
          </div>
        ) : null}
        <div className="screen-bar">
          <span className={`presence ${ready ? 'is-live' : ''}`} />
          {ready ? 'Visible Chrome attached' : 'Local computer idle'}
        </div>
      </div>

      <div className="computer-metrics">
        <div>
          <IconBrandChrome size={16} />
          <span>
            Browser
            <strong>{status?.readiness.browser || 'local'}</strong>
          </span>
        </div>
        <div>
          <IconActivity size={16} />
          <span>
            Tabs
            <strong>{status?.readiness.tab_count ?? '—'}</strong>
          </span>
        </div>
      </div>

      <section className="profile-card">
        <AbstractAvatar name={displayName} seed={coworker.id} size={38} />
        <div>
          <strong>{displayName}</strong>
          <span>{coworker.description || coworker.id}</span>
        </div>
      </section>

      <section className="authority-card">
        <div className="authority-title">
          <IconLock size={15} /> Human control boundary
        </div>
        <p>
          The donor takeover UI lands here only after Homie's digest-bound ApprovalGrant
          is enforced by the backend.
        </p>
        <button type="button" disabled>
          Take the wheel · locked
        </button>
      </section>
    </aside>
  );
}
