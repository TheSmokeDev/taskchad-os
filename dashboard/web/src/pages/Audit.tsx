import { Placeholder } from './Placeholder';

export function Audit() {
  return (
    <Placeholder
      title="Audit"
      description="Action log + framework audit_log view ships in a later phase. Hard-delete events already write audit rows server-side via the framework — this page surfaces them."
    />
  );
}
