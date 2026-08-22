# docs(agencies): add Graphviz flow diagrams for the agencies module

## Summary

Four `.dot` sources (with rendered `.png` + `.svg`) walking through every
endpoint of the SUPER_ADMIN-only `/agencies` module introduced in the
previous PR. Renders match the dark-theme Graphviz style already used in
the team's docs (dark `#0f1115` background, START/END circles, step
boxes, dotted lines for side-effect references).

## Diagrams

| File | What it covers |
|---|---|
| `agencies_flow_create.dot` | `POST /agencies` → `GET one` → `GET programs`. Includes role-gate (SUPER_ADMIN only), Pydantic validation (blank name, name>255, unknown program code), the `agency_programs` insert branch, and the `audit_logs` row written for the CREATE action. |
| `agencies_flow_patch.dot`  | `PATCH {status: SUSPENDED}` → `PATCH {status: ACTIVE}`. Walks the four `new_status` branches in `update_agency()` — `SUSPENDED` stamps `settings.suspended_at`, `CHURNED` stamps `settings.churned_at`, `ACTIVE`/`TRIAL` clears `suspended_at` and stamps `reactivated_at`, and `None` is the no-op rename/timezone/settings branch. |
| `agencies_flow_delete.dot` | `DELETE` → read-default → `404` → `?include_deleted=true` → `200`. Shows the soft-delete behaviour (row preserved for FK references), idempotent re-delete, and the audit row written with `action=DELETE, new_data={soft_delete: true}`. |
| `agencies_flow_list.dot`   | `GET /agencies` with `page`, `page_size`, `include_deleted`, `status_filter`. Walks filter chaining, count + offset/limit pagination, the RLS bypass path for SUPER_ADMIN, and the `build_offset_response` envelope shape. |

Each diagram shows:
- request → service → DB → response happy path (green)
- error paths: 401, 403, 404, 422, 409 (red)
- side-effect references (audit row, table writes) via dotted lines
- START / END circles
- per-request status codes and response-body snippets

## Why

Reviewers and new contributors had to read the service file end-to-end to
understand the module. These diagrams let docs readers — and future
Postman-collection authors — see the full request → service → DB → audit →
response chain at a glance, with all error branches visible.

## Files

```
docs/flow/
├── agencies_flow_create.{dot,png,svg}
├── agencies_flow_patch.{dot,png,svg}
├── agencies_flow_delete.{dot,png,svg}
└── agencies_flow_list.{dot,png,svg}
```

12 new files, 1661 lines (most of it Graphviz text — the images are
small PNGs and inline SVGs).

## How to regenerate

```bash
cd docs/flow
for f in agencies_flow_*.dot; do
  dot -Tpng "$f" -o "${f%.dot}.png"
  dot -Tsvg "$f" -o "${f%.dot}.svg"
done
```

Requires `graphviz` (`apt install graphviz`).

## Checklist

- [x] All 4 diagrams render without warnings
- [x] Style consistent with existing repo conventions (Graphviz dark theme)
- [x] No code changes — docs only, safe to merge
- [x] Lint/typecheck untouched (no Python files changed)
