declare module 'd3-force-3d' {
  export interface SimulationNodeDatum {
    index?: number;
    x?: number;
    y?: number;
    z?: number;
    vx?: number;
    vy?: number;
    vz?: number;
    fx?: number | null;
    fy?: number | null;
    fz?: number | null;
  }

  export interface SimulationLinkDatum<NodeDatum extends SimulationNodeDatum> {
    source: string | number | NodeDatum;
    target: string | number | NodeDatum;
    index?: number;
  }

  export interface Force<NodeDatum extends SimulationNodeDatum, LinkDatum extends SimulationLinkDatum<NodeDatum> = SimulationLinkDatum<NodeDatum>> {
    (alpha: number): void;
    initialize?(nodes: NodeDatum[], random?: () => number, dimensions?: number): void;
  }

  export interface Simulation<NodeDatum extends SimulationNodeDatum, LinkDatum extends SimulationLinkDatum<NodeDatum> = SimulationLinkDatum<NodeDatum>> {
    restart(): this;
    stop(): this;
    tick(iterations?: number): this;
    nodes(): NodeDatum[];
    nodes(nodes: NodeDatum[]): this;
    alpha(): number;
    alpha(alpha: number): this;
    alphaMin(): number;
    alphaMin(alpha: number): this;
    alphaDecay(): number;
    alphaDecay(decay: number): this;
    alphaTarget(): number;
    alphaTarget(target: number): this;
    velocityDecay(): number;
    velocityDecay(decay: number): this;
    force(name: string): Force<NodeDatum, LinkDatum> | undefined;
    force(name: string, force: Force<NodeDatum, LinkDatum> | null): this;
    numDimensions(): number;
    numDimensions(dimensions: 1 | 2 | 3): this;
  }

  export interface LinkForce<NodeDatum extends SimulationNodeDatum, LinkDatum extends SimulationLinkDatum<NodeDatum>> extends Force<NodeDatum, LinkDatum> {
    links(): LinkDatum[];
    links(links: LinkDatum[]): this;
    id(): (node: NodeDatum, index: number, nodes: NodeDatum[]) => string | number;
    id(id: (node: NodeDatum, index: number, nodes: NodeDatum[]) => string | number): this;
    distance(): (link: LinkDatum, index: number, links: LinkDatum[]) => number;
    distance(distance: number | ((link: LinkDatum, index: number, links: LinkDatum[]) => number)): this;
    strength(): (link: LinkDatum, index: number, links: LinkDatum[]) => number;
    strength(strength: number | ((link: LinkDatum, index: number, links: LinkDatum[]) => number)): this;
    iterations(): number;
    iterations(iterations: number): this;
  }

  export interface ManyBodyForce<NodeDatum extends SimulationNodeDatum> extends Force<NodeDatum> {
    strength(): (node: NodeDatum, index: number, nodes: NodeDatum[]) => number;
    strength(strength: number | ((node: NodeDatum, index: number, nodes: NodeDatum[]) => number)): this;
    distanceMin(): number;
    distanceMin(distance: number): this;
    distanceMax(): number;
    distanceMax(distance: number): this;
    theta(): number;
    theta(theta: number): this;
  }

  export interface PositionForce<NodeDatum extends SimulationNodeDatum> extends Force<NodeDatum> {
    strength(): (node: NodeDatum, index: number, nodes: NodeDatum[]) => number;
    strength(strength: number | ((node: NodeDatum, index: number, nodes: NodeDatum[]) => number)): this;
  }

  export interface CollideForce<NodeDatum extends SimulationNodeDatum> extends Force<NodeDatum> {
    radius(): (node: NodeDatum, index: number, nodes: NodeDatum[]) => number;
    radius(radius: number | ((node: NodeDatum, index: number, nodes: NodeDatum[]) => number)): this;
    strength(): number;
    strength(strength: number): this;
    iterations(): number;
    iterations(iterations: number): this;
  }

  export function forceSimulation<NodeDatum extends SimulationNodeDatum>(nodes?: NodeDatum[], numDimensions?: 1 | 2 | 3): Simulation<NodeDatum>;
  export function forceLink<NodeDatum extends SimulationNodeDatum, LinkDatum extends SimulationLinkDatum<NodeDatum>>(links?: LinkDatum[]): LinkForce<NodeDatum, LinkDatum>;
  export function forceManyBody<NodeDatum extends SimulationNodeDatum>(): ManyBodyForce<NodeDatum>;
  export function forceCollide<NodeDatum extends SimulationNodeDatum>(radius?: number | ((node: NodeDatum, index: number, nodes: NodeDatum[]) => number)): CollideForce<NodeDatum>;
  export function forceX<NodeDatum extends SimulationNodeDatum>(x?: number | ((node: NodeDatum, index: number, nodes: NodeDatum[]) => number)): PositionForce<NodeDatum>;
  export function forceY<NodeDatum extends SimulationNodeDatum>(y?: number | ((node: NodeDatum, index: number, nodes: NodeDatum[]) => number)): PositionForce<NodeDatum>;
  export function forceZ<NodeDatum extends SimulationNodeDatum>(z?: number | ((node: NodeDatum, index: number, nodes: NodeDatum[]) => number)): PositionForce<NodeDatum>;
}
