import { TopBar } from '@/components/TopBar';
import { BrainGraph3D } from '@/components/BrainGraph3D';

export function HiveMind() {
  return (
    <div class="flex flex-col h-full">
      <TopBar title="Hive Mind" subtitle="Recent inter-agent activity" />
      <div class="flex-1 min-h-0 p-4">
        <BrainGraph3D limit={200} />
      </div>
    </div>
  );
}
