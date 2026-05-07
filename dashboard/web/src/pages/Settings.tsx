import { TopBar } from '@/components/TopBar';
import { useFetch } from '@/lib/useFetch';
import { apiPatch } from '@/lib/api';
import { theme, themeMeta, setTheme, showCosts, setShowCosts, type ThemeName } from '@/lib/theme';
import { pushToast } from '@/lib/toasts';

interface DashboardSettings {
  workspace_name?: string;
  hotkey_mod?: 'meta' | 'ctrl' | 'auto';
}

export function Settings() {
  const settings = useFetch<DashboardSettings>('/api/dashboard/settings');

  async function updateSetting(key: string, value: string) {
    try {
      await apiPatch('/api/dashboard/settings', { key, value });
      pushToast({ tone: 'success', title: 'Saved' });
      settings.refresh();
    } catch (err: any) {
      pushToast({ tone: 'error', title: 'Save failed', description: err?.message || String(err) });
    }
  }

  return (
    <div class="flex flex-col h-full">
      <TopBar title="Settings" />
      <div class="flex-1 overflow-y-auto p-6 max-w-2xl space-y-6">
        <section>
          <h3 class="text-[11px] uppercase tracking-wider text-[var(--color-text-faint)] mb-3">Display</h3>
          <div class="space-y-3">
            <Field label="Theme">
              <div class="flex items-center gap-2">
                {(Object.keys(themeMeta) as ThemeName[]).map((name) => (
                  <button
                    key={name}
                    type="button"
                    onClick={() => setTheme(name)}
                    class={[
                      'px-3 py-1.5 rounded text-[12px] border transition-colors',
                      theme.value === name
                        ? 'bg-[var(--color-accent-soft)] text-[var(--color-accent)] border-[var(--color-accent)]'
                        : 'bg-[var(--color-elevated)] text-[var(--color-text-muted)] border-[var(--color-border)] hover:text-[var(--color-text)]',
                    ].join(' ')}
                  >
                    {themeMeta[name].label}
                  </button>
                ))}
              </div>
            </Field>
            <Field label="Show costs">
              <label class="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={showCosts.value}
                  onChange={(e) => setShowCosts((e.target as HTMLInputElement).checked)}
                  class="cursor-pointer"
                />
                <span class="text-[12px] text-[var(--color-text-muted)]">
                  Display per-message cost (only meaningful on the API path)
                </span>
              </label>
            </Field>
          </div>
        </section>

        <section>
          <h3 class="text-[11px] uppercase tracking-wider text-[var(--color-text-faint)] mb-3">Workspace</h3>
          <Field label="Name">
            <input
              type="text"
              value={settings.data?.workspace_name ?? ''}
              onChange={(e) => updateSetting('workspace_name', (e.target as HTMLInputElement).value)}
              placeholder="The Homie"
              class="w-full bg-[var(--color-elevated)] border border-[var(--color-border)] rounded px-2.5 py-1.5 text-[12.5px] text-[var(--color-text)] outline-none focus:border-[var(--color-accent)]"
            />
          </Field>
        </section>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: any }) {
  return (
    <div>
      <label class="block text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">{label}</label>
      {children}
    </div>
  );
}
