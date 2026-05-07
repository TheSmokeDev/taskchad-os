import { Loader2 } from 'lucide-preact';

export function Spinner({ size = 16, className = '' }: { size?: number; className?: string }) {
  return <Loader2 size={size} class={`animate-spin ${className}`} />;
}
