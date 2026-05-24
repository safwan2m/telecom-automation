# CLAUDE.md — Telecom Automation Project

## Project Overview

AI agent system for automated 4G/5G network planning and deployment. Accepts geographic/operational parameters and autonomously plans RU/DU/CU placement, generates Kubernetes manifests, deploys to SMO, and continuously optimizes KPIs.

See `spec.md` for full specification and `prerequisites.md` for background knowledge required.

## Repository Structure (planned)

```
telecom-automation/
├── agents/
│   ├── orchestrator/      # LLM chat agent + tool routing
│   ├── core_db/           # Database agent (CRUD for cells, components, UEs)
│   ├── deployment/        # Manifest generation + K8s deployment + SMO registration
│   └── kpi_monitor/       # KPI polling, power/profit optimization, alerting
├── planning/
│   ├── placement/         # RU/DU/CU placement algorithms
│   ├── pci/               # PCI planning
│   ├── routing/           # Fronthaul/midhaul routing
│   └── slicing/           # Network slice allocation
├── manifests/
│   └── templates/         # Helm/K8s templates per O-RAN component
├── db/
│   └── schema/            # Core DB schema definitions
├── tests/
│   ├── unit/
│   └── integration/
├── data/                  # Sample geographic data, telecom data
├── spec.md
├── prerequisites.md
└── CLAUDE.md
```

## Key Design Decisions

- **Orchestrator uses tool-calling**: each sub-agent exposes a typed tool interface; the LLM never directly writes to the DB or cluster — it calls tools.
- **Core DB is the single source of truth**: all agents read current deployment state from the DB before acting; no agent holds local state between calls.
- **Idempotent deployments**: manifest agent must handle re-runs safely (apply, not create).
- **KPI agent is a separate process**: it runs continuously on a polling loop, independent of the chat agent.

## Coding Conventions

- Language: Python (primary), YAML for manifests
- Use `pydantic` for all data models (input params, DB schemas, agent tool signatures)
- Use `anthropic` SDK for LLM calls; always include prompt caching headers
- Async-first: use `asyncio` / `httpx` for all I/O
- Each agent module must expose a `run(input: AgentInput) -> AgentOutput` interface
- Do not hardcode credentials — use environment variables or a secrets manager

## Environment Variables Required

```
ANTHROPIC_API_KEY=        # LLM backend
DATABASE_URL=             # Core DB connection string
KUBECONFIG=               # Path to kubeconfig for deployment agent
SMO_BASE_URL=             # SMO northbound API endpoint
SMO_AUTH_TOKEN=           # SMO authentication token
KPI_POLL_INTERVAL_SEC=30  # KPI monitoring interval
```

## Running Tests

```bash
pytest tests/unit/
pytest tests/integration/   # requires a running DB and mock SMO
```

## Deadline

All tasks in `spec.md` must be completed by **15 June 2026**.
