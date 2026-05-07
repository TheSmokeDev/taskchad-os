import { useEffect, useRef } from 'preact/hooks';
import * as THREE from 'three';
import { useFetch } from '@/lib/useFetch';
import { hasWebGL } from '@/lib/webgl';
import { BrainGraph } from './BrainGraph';

interface HiveMindEvent {
  id: string;
  personaId: string;
  type: string;
  timestamp: number;
}
interface HiveMindResponse { events: HiveMindEvent[]; }

/**
 * 3D anatomical "brain" visualization of recent HiveMind events. Each
 * persona id maps to a deterministic position on a sphere; events pulse
 * the corresponding node. WebGL probe falls back to BrainGraph (2D list).
 */
export function BrainGraph3D({ personaId, limit = 200 }: { personaId?: string; limit?: number }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<{ scene: THREE.Scene; renderer: THREE.WebGLRenderer; nodes: Map<string, THREE.Mesh>; cleanup: () => void } | null>(null);

  const params = new URLSearchParams();
  if (personaId) params.set('persona_id', personaId);
  params.set('limit', String(limit));
  params.set('window_minutes', '60');
  const { data } = useFetch<HiveMindResponse>(`/api/hive-mind/recent?${params.toString()}`, 5_000);

  useEffect(() => {
    if (!hasWebGL() || !containerRef.current) return;

    const container = containerRef.current;
    const w = container.clientWidth;
    const h = container.clientHeight || 400;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 1000);
    camera.position.set(0, 0, 8);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(w, h);
    container.appendChild(renderer.domElement);

    // Outer wireframe sphere (the cortex shell).
    const cortex = new THREE.Mesh(
      new THREE.SphereGeometry(2.6, 32, 24),
      new THREE.MeshBasicMaterial({ color: 0x444466, wireframe: true, transparent: true, opacity: 0.25 }),
    );
    scene.add(cortex);

    const nodes = new Map<string, THREE.Mesh>();

    let raf = 0;
    const start = performance.now();
    function animate() {
      const t = (performance.now() - start) / 1000;
      cortex.rotation.y = t * 0.05;
      cortex.rotation.x = Math.sin(t * 0.07) * 0.1;
      nodes.forEach((mesh) => {
        const m = mesh.material as THREE.MeshBasicMaterial;
        const userData = mesh.userData as { lastEventAt?: number };
        if (userData.lastEventAt) {
          const elapsed = (Date.now() - userData.lastEventAt) / 1000;
          const pulse = Math.max(0, 1 - elapsed / 2);
          m.opacity = 0.4 + pulse * 0.6;
        }
      });
      renderer.render(scene, camera);
      raf = requestAnimationFrame(animate);
    }
    animate();

    function onResize() {
      const w2 = container.clientWidth;
      const h2 = container.clientHeight || 400;
      renderer.setSize(w2, h2);
      camera.aspect = w2 / h2;
      camera.updateProjectionMatrix();
    }
    window.addEventListener('resize', onResize);

    sceneRef.current = {
      scene,
      renderer,
      nodes,
      cleanup() {
        cancelAnimationFrame(raf);
        window.removeEventListener('resize', onResize);
        renderer.dispose();
        if (renderer.domElement.parentElement === container) {
          container.removeChild(renderer.domElement);
        }
      },
    };

    return () => sceneRef.current?.cleanup();
  }, []);

  // Update nodes when events arrive.
  useEffect(() => {
    if (!sceneRef.current || !data?.events) return;
    const { scene, nodes } = sceneRef.current;
    for (const ev of data.events) {
      let node = nodes.get(ev.personaId);
      if (!node) {
        const pos = positionForPersona(ev.personaId);
        node = new THREE.Mesh(
          new THREE.SphereGeometry(0.12, 12, 8),
          new THREE.MeshBasicMaterial({ color: 0x8b8af0, transparent: true, opacity: 0.5 }),
        );
        node.position.copy(pos);
        scene.add(node);
        nodes.set(ev.personaId, node);
      }
      node.userData.lastEventAt = ev.timestamp * 1000;
    }
  }, [data]);

  if (!hasWebGL()) {
    return <BrainGraph personaId={personaId} limit={limit} />;
  }

  return <div ref={containerRef} class="w-full h-full min-h-[400px]" />;
}

/** Deterministically place a persona id on a sphere via hash. */
function positionForPersona(id: string): THREE.Vector3 {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) & 0xffffffff;
  const theta = ((h & 0xffff) / 0xffff) * Math.PI * 2;
  const phi = (((h >> 16) & 0xffff) / 0xffff) * Math.PI;
  const r = 2.4;
  return new THREE.Vector3(
    r * Math.sin(phi) * Math.cos(theta),
    r * Math.cos(phi),
    r * Math.sin(phi) * Math.sin(theta),
  );
}
