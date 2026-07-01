# Forge Documentation

Welcome to the Forge documentation. This suite is designed for senior developers joining the project and covers everything from architecture to deployment.

## Table of Contents

| # | Document | Description |
|---|----------|-------------|
| 1 | [Overview](./01-OVERVIEW.md) | What Forge is, current status, and tech stack |
| 2 | [Architecture](./02-ARCHITECTURE.md) | 6-layer design, event bus, data flow |
| 3 | [Runtime Modules](./03-RUNTIME-MODULES.md) | All 27 runtime modules with APIs and events |
| 4 | [Workflow](./04-WORKFLOW.md) | LangGraph state machine, nodes, routing |
| 5 | [Adapters](./05-ADAPTERS.md) | OpenRouter, GitHub VCS, Aider, Sandboxed Aider |
| 6 | [Database](./06-DATABASE.md) | PostgreSQL schema, stores, migrations |
| 7 | [Frontend](./07-FRONTEND.md) | Next.js UI, components, WebSocket |
| 8 | [Deployment](./08-DEPLOYMENT.md) | Docker Compose, env vars, scaling |
| 9 | [Testing](./09-TESTING.md) | Test strategy, property-based testing, coverage |
| 10 | [Future](./10-FUTURE.md) | Roadmap, limitations, tradeoffs |
| 11 | [File Structure](./11-FILE-STRUCTURE.md) | Complete project file tree |
| 12 | [Security](./12-SECURITY.md) | Workspace sandboxing, scope checks, secret isolation |
| 13 | [Troubleshooting](./13-TROUBLESHOOTING.md) | Common issues, solutions, and debugging tips |

## Quick Navigation

- **New to the project?** Start with [Overview](./01-OVERVIEW.md) then [Architecture](./02-ARCHITECTURE.md).
- **Working on runtime logic?** See [Runtime Modules](./03-RUNTIME-MODULES.md) and [Workflow](./04-WORKFLOW.md).
- **Adding an integration?** See [Adapters](./05-ADAPTERS.md).
- **Setting up locally?** See [Deployment](./08-DEPLOYMENT.md) and the root [README](../README.md).
- **Writing tests?** See [Testing](./09-TESTING.md).
- **Security concerns?** See [Security](./12-SECURITY.md).

## Conventions

- All code examples reference actual files in the repository.
- Mermaid diagrams are used for architecture and flow visualization.
- Each document is self-contained and can be read independently.
