import { IconArrowUp, IconBolt, IconLoader2, IconRefresh } from '@tabler/icons-react';
import { useEffect, useRef, useState } from 'react';
import { AbstractAvatar } from '@/components/AbstractAvatar';
import { coworkerDisplayName, type ChatEvent, type Coworker } from '@/types';

interface Props {
  coworker: Coworker;
  events: ChatEvent[];
  connected: boolean;
  sending: boolean;
  error: string | null;
  onSend: (text: string, buttonCustomId?: string) => Promise<void>;
  onRefresh: () => Promise<void>;
}

function timeLabel(timestamp: number): string {
  const date = new Date(timestamp * 1000);
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function ActivityLine({ event }: { event: ChatEvent }) {
  return (
    <div className={`activity-line ${event.type === 'error' ? 'is-error' : ''}`}>
      <span className="activity-icon">
        {event.type === 'error' ? '!' : <IconBolt size={13} />}
      </span>
      <span>{event.text || (event.type === 'processing' ? 'Working…' : 'Activity update')}</span>
      <span className="activity-time">{timeLabel(event.timestamp)}</span>
    </div>
  );
}

export function ConversationPane({
  coworker,
  events,
  connected,
  sending,
  error,
  onSend,
  onRefresh,
}: Props) {
  const displayName = coworkerDisplayName(coworker);
  const [draft, setDraft] = useState('');
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [events.length]);

  const submit = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    await onSend(text);
    setDraft('');
  };

  return (
    <section className="conversation-pane">
      <header className="conversation-header">
        <div className="conversation-identity">
          <AbstractAvatar name={displayName} seed={coworker.id} size={32} />
          <div>
            <strong>{displayName}</strong>
            <span>
              <i className={`connection-dot ${connected ? 'is-connected' : ''}`} />
              {connected ? 'live local stream' : 'reconnecting'} · {coworker.lane ?? 'runtime'}
            </span>
          </div>
        </div>
        <button className="icon-button" type="button" onClick={() => void onRefresh()} aria-label="Refresh history">
          <IconRefresh size={17} />
        </button>
      </header>

      <div className="transcript" ref={scrollRef}>
        {events.length === 0 ? (
          <div className="empty-conversation">
            <AbstractAvatar name={displayName} seed={coworker.id} size={58} />
            <h1>Talk to {displayName}</h1>
            <p>{coworker.description || 'This Homie persona is ready for a local channel.'}</p>
            <div className="prompt-grid">
              <button type="button" onClick={() => setDraft('What are you working on right now?')}>
                Current focus
                <small>Ask for live persona context</small>
              </button>
              <button type="button" onClick={() => setDraft('Show me what you can help with.') }>
                Capabilities
                <small>See the granted tool boundary</small>
              </button>
            </div>
          </div>
        ) : null}

        {events.map((event) => {
          if (event.type === 'processing' || event.type === 'progress' || event.type === 'error') {
            return <ActivityLine key={event.id} event={event} />;
          }
          const fromUser = event.type === 'user_message';
          return (
            <article key={event.id} className={`message-row ${fromUser ? 'is-user' : 'is-coworker'}`}>
              {!fromUser ? (
                <AbstractAvatar name={displayName} seed={coworker.id} size={28} />
              ) : null}
              <div className="message-stack">
                <div className="message-bubble">{event.text}</div>
                {event.components.length > 0 ? (
                  <div className="action-row">
                    {event.components.map((component) => (
                      <button
                        key={component.custom_id}
                        type="button"
                        disabled={
                          component.disabled ||
                          sending ||
                          pendingAction === component.custom_id
                        }
                        className={component.style === 'primary' ? 'is-primary' : ''}
                        onClick={async () => {
                          setPendingAction(component.custom_id);
                          try {
                            await onSend('', component.custom_id);
                          } finally {
                            setPendingAction(null);
                          }
                        }}
                      >
                        {pendingAction === component.custom_id ? 'Sent' : component.label}
                      </button>
                    ))}
                  </div>
                ) : null}
                <time>{timeLabel(event.timestamp)}</time>
              </div>
            </article>
          );
        })}
      </div>

      <footer className="composer-wrap">
        {error ? <div className="conversation-error">{error}</div> : null}
        <form
          className="composer"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <textarea
            aria-label={`Message ${displayName}`}
            placeholder={`Message ${displayName}…`}
            rows={1}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                void submit();
              }
            }}
          />
          <button type="submit" disabled={sending || !draft.trim()} aria-label="Send message">
            {sending ? <IconLoader2 className="spin" size={18} /> : <IconArrowUp size={18} />}
          </button>
        </form>
        <p>Homie runtime · local session · exact actions stay approval-gated</p>
      </footer>
    </section>
  );
}
