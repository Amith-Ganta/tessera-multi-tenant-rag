# Architecture Decision Records

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-001](ADR-001.md) | LiteLLM Provider Abstraction over Direct Provider SDKs | Accepted |
| [ADR-002](ADR-002.md) | Google A2A Protocol for Inter-Agent Communication | Accepted |
| [ADR-003](ADR-003.md) | Conditional Cross-Encoder Re-ranking at a 0.90 Similarity Threshold | Accepted |
| [ADR-004](ADR-004.md) | Per-Tenant Vectorstore Caching with Double-Checked Locking | Accepted |
| [ADR-005](ADR-005.md) | Merge-Blocking Evaluation Gate in CI | Accepted |
| [ADR-006](ADR-006.md) | In-Process Async Judging Is a Stopgap, Not a Concurrency Model | Superseded by ADR-007 |
| [ADR-007](ADR-007.md) | Redis Message Queue for Judge Evaluation Jobs | Accepted |
| [ADR-008](ADR-008.md) | Rate Limiting, Circuit Breaker, and Bulkhead | Accepted |
| [ADR-009](ADR-009.md) | Redis Checkpointer for Stateless A2A Agents | Accepted |
| [ADR-010](ADR-010.md) | Autoscaling Strategy: HPA on Queue Depth for Judge Workers | Accepted |
| [ADR-011](ADR-011.md) | Canary deployment for model version changes | Accepted |
| [ADR-012](ADR-012.md) | LLM Deployment Governance | Accepted |
| [ADR-014](ADR-014.md) | Circuit Breaker State Scope — In-Process Only | Accepted |

ADRs follow the [Nygard format](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions): Context, Decision, Alternatives Considered, Consequences.
