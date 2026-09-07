"""Enterprise SMS Control Plane -- domain package.

See DECISIONS.md (2026-09-07, "Enterprise SMS Control Plane") for the
scoping rationale: a genuinely separate product from Bingo, sharing this
platform's existing admin auth/RBAC/audit/Postgres/Redis infrastructure
rather than growing a second one.

Every function in this package takes an explicit tenant_id and never
infers it from ambient state -- the directive's own "tenant isolation
always" principle, enforced at the query level in every module here, not
just at the API layer.
"""
