# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code under frontend/.

Vite + React 18 + TS, React Flow v11 (`reactflow` package). [src/api.ts](src/api.ts) holds typed fetch wrappers for every endpoint (`VITE_API_URL ?? http://localhost:8000`; the admin key is never bundled — the operator enters it at runtime and it is kept only in `sessionStorage`, sent as `X-Admin-Key` on dashboard requests, since backend GETs are admin-gated too). [src/App.tsx](src/App.tsx) polls events/graph/runs/resources every 1.5s; components are `EventFeed`, `LineageGraph` (run vs resource nodes, red blocked edges), `RunsPanel` (suspend/reactivate + each run's effective context label), `SimulatorPanel` ("Run Flow Demo" step results). No build-time API calls — `npm run build` (`tsc && vite build`) must pass with the backend NOT running.
