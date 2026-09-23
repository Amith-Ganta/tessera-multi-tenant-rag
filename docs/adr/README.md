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
| [ADR-013](ADR-013.md) | Tenant Identity Derivation from User ID | Accepted |
| [ADR-014](ADR-014.md) | Circuit Breaker State Scope — In-Process Only | Accepted |
| [ADR-015](ADR-015.md) | Fail-Open Rate Limiter vs Fail-Closed Tenant Governor | Accepted |
| [ADR-016](ADR-016.md) | Per-Tenant Resource Governance via Redis Counters | Accepted |
| [ADR-017](ADR-017.md) | Five-Threshold Quality Gate | Accepted |
| [ADR-018](ADR-018.md) | Centralised Cost Observability Module | Accepted |
| [ADR-019](ADR-019.md) | Shadow Evaluation and Fail-Closed Promotion Gate | Accepted |
| [ADR-020](ADR-020.md) | Structured RAG Quality Signals as a Separate Observability Record Type | Accepted |
| [ADR-021](ADR-021.md) | At-Most-Once Judge Queue Delivery | Accepted |

ADRs follow the [Nygard format](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions): Context, Decision, Alternatives Considered, Consequences.
