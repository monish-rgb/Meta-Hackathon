import { useMemo } from 'react';
import ReactFlow, { Background, Controls } from 'reactflow';
import type { Edge, Node } from 'reactflow';
import 'reactflow/dist/style.css';
import type { GraphEdge, GraphNode } from '../api';

interface Props {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export default function LineageGraph({ nodes, edges }: Props) {
  const rfNodes: Node[] = useMemo(() => {
    const runs = nodes.filter((n) => n.type === 'run');
    const resources = nodes.filter((n) => n.type !== 'run');

    const toNode = (n: GraphNode, x: number, y: number): Node => {
      const isRun = n.type === 'run';
      const suspended = isRun && n.suspended === true;
      const sub = isRun
        ? `project=${n.project ?? '?'} clearance=${n.clearance ?? '?'}${suspended ? ' · SUSPENDED' : ''}`
        : `project=${n.project ?? '?'} sensitivity=${n.sensitivity ?? '?'}`;
      return {
        id: n.id,
        position: { x, y },
        data: {
          label: (
            <div>
              <div className="node-title">
                {suspended ? `suspended ${n.label}` : n.label}
              </div>
              <div className="node-sub">{sub}</div>
              <div className="node-kind">{isRun ? 'run' : 'resource'}</div>
            </div>
          ),
        },
        style: {
          background: suspended ? '#3d1a1a' : isRun ? '#1e3a5f' : '#1f3d2b',
          border: `1px solid ${suspended ? '#ff5a5a' : isRun ? '#4da3ff' : '#4caf7d'}`,
          borderRadius: isRun ? 12 : 6,
          color: '#e6edf3',
          padding: 8,
          width: 230,
        },
      };
    };

    return [
      ...resources.map((n, i) => toNode(n, 0, i * 140)),
      ...runs.map((n, i) => toNode(n, 440, i * 140)),
    ];
  }, [nodes]);

  const rfEdges: Edge[] = useMemo(
    () =>
      edges.map((e: GraphEdge) => {
        const isSpawn = e.operation === 'spawn';
        const isBlocked = e.decision === 'blocked';
        return {
          id: e.id,
          source: e.source,
          target: e.target,
          label: isSpawn
            ? e.policy_id
              ? `spawn · ${e.policy_id}`
              : 'spawn'
            : `${e.operation} · ${e.policy_id}`,
          animated: isBlocked,
          style: {
            stroke: isBlocked ? '#ff5a5a' : isSpawn ? '#c586ff' : '#4caf7d',
            strokeWidth: 2,
            ...(isSpawn ? { strokeDasharray: '6 4' } : {}),
          },
        };
      }),
    [edges],
  );

  if (nodes.length === 0) {
    return <p className="muted">No lineage yet — run the attack demo.</p>;
  }

  return (
    <div className="graph-wrap">
      <div className="legend">
        <span className="legend-run">run</span>
        <span className="legend-resource">resource</span>
        <span className="legend-blocked">blocked edge</span>
        <span className="legend-spawn">spawn edge (dashed)</span>
        <span className="muted">
          reads: resource → run · writes: run → resource · spawns: run → run
        </span>
      </div>
      <div style={{ height: 420 }}>
        <ReactFlow nodes={rfNodes} edges={rfEdges} fitView>
          <Background />
          <Controls />
        </ReactFlow>
      </div>
    </div>
  );
}
