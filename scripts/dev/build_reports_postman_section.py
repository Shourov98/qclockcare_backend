"""Build the reports section JSON for the Postman collection.

Run from /home/shourov/Documents/work_projects/QlockCare/qclockcare_backend:

    uv run python scripts/dev/build_reports_postman_section.py

Reads `postman/QlockCare_API.postman_collection.json`, splices the
new "reports" section before the closing `]` of the top-level `item`
array, writes the result back atomically, and validates the JSON.

This is a dev-only script (idempotent — running twice is a no-op).
It lives under `scripts/dev/` rather than `scripts/` because it
mutates a hand-maintained artifact; treat the collection file as
source-controlled and review the diff like any other code change.
"""

from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path

# Repo-relative path so the script works no matter where it's
# invoked from.
COLLECTION_PATH = Path(__file__).resolve().parents[2] / "postman" / "QlockCare_API.postman_collection.json"

REPORTS_SECTION: dict = {
    "name": "reports",
    "description": "AI-generated narrative reports powered by Claude. POST /reports/{type}/stream opens an SSE stream of the narrative; GET /reports/runs[/{id}] returns the persisted history; GET /reports/runs/{id}/export renders the snapshot as PDF/CSV/XLSX. **Requires CLAUDE_API_KEY** — disable with FEATURE_REPORTS_AI_NARRATIVE=false. Rate-limited to 5/minute per IP.",
    "item": [
        # 1. Stream Claude narrative (POST /reports/{type}/stream)
        {
            "name": "Stream Claude narrative — POST /reports/{type}/stream",
            "request": {
                "method": "POST",
                "header": [
                    {"key": "Content-Type", "value": "application/json", "type": "text"},
                    {"key": "X-Request-ID", "value": "{{$randomUUID}}", "type": "text"},
                ],
                "url": {
                    "raw": "{{base_url}}/reports/visit_summary/stream",
                    "host": ["{{base_url}}"],
                    "path": ["reports", "visit_summary", "stream"],
                },
                "auth": {
                    "type": "bearer",
                    "bearer": [
                        {"key": "token", "value": "{{access_token}}", "type": "string"}
                    ],
                },
                "body": {
                    "mode": "raw",
                    "raw": (
                        "{\n"
                        "  \"params\": {\n"
                        "    \"date_from\": \"2026-08-01\",\n"
                        "    \"date_to\": \"2026-08-11\"\n"
                        "  }\n"
                        "}"
                    ),
                    "options": {"raw": {"language": "json"}},
                },
            },
            "response": [],
            "event": [
                {
                    "listen": "test",
                    "script": {
                        "type": "text/javascript",
                        "exec": [
                            "pm.test('Stream Claude narrative — status is in 2xx', () => {",
                            "    pm.expect(pm.response.code, `expected 2xx, got ${pm.response.code}: ${pm.response.text()}`).to.be.within(200, 299);",
                            "});",
                            "",
                            "pm.test('Stream Claude narrative — SSE content type', () => {",
                            "    const ct = pm.response.headers.get('Content-Type') || '';",
                            "    pm.expect(ct, `expected text/event-stream, got ${ct}`).to.include('text/event-stream');",
                            "});",
                            "",
                            "pm.test('Stream Claude narrative — X-Request-ID round-trip', () => {",
                            "    const rid = pm.response.headers.get('X-Request-ID');",
                            "    pm.expect(rid, 'X-Request-ID header missing').to.be.a('string');",
                            "    pm.expect(rid.length, 'X-Request-ID empty').to.be.greaterThan(0);",
                            "});",
                        ],
                    },
                }
            ],
        },
        # 2. List report runs (GET /reports/runs)
        {
            "name": "List report runs — GET /reports/runs",
            "request": {
                "method": "GET",
                "header": [
                    {"key": "Content-Type", "value": "application/json", "type": "text"},
                    {"key": "X-Request-ID", "value": "{{$randomUUID}}", "type": "text"},
                ],
                "url": {
                    "raw": "{{base_url}}/reports/runs?limit=20",
                    "host": ["{{base_url}}"],
                    "path": ["reports", "runs"],
                    "query": [{"key": "limit", "value": "20"}],
                },
                "auth": {
                    "type": "bearer",
                    "bearer": [
                        {"key": "token", "value": "{{access_token}}", "type": "string"}
                    ],
                },
            },
            "response": [],
            "event": [
                {
                    "listen": "test",
                    "script": {
                        "type": "text/javascript",
                        "exec": [
                            "pm.test('List report runs — status is in 2xx', () => {",
                            "    pm.expect(pm.response.code, `expected 2xx, got ${pm.response.code}: ${pm.response.text()}`).to.be.within(200, 299);",
                            "});",
                            "",
                            "pm.test('List report runs — response envelope shape', () => {",
                            "    const b = pm.response.json();",
                            "    if (pm.response.code >= 400) {",
                            "        pm.expect(b, 'error envelope missing').to.have.property('error');",
                            "        pm.expect(b.error).to.have.property('code');",
                            "        pm.expect(b.error).to.have.property('message');",
                            "        pm.expect(b.error).to.have.property('request_id');",
                            "        pm.expect(b.error).to.have.property('timestamp');",
                            "    } else {",
                            "        pm.expect(b, 'success envelope missing data').to.have.property('data');",
                            "        pm.expect(Array.isArray(b.data), 'data must be a list').to.be.true;",
                            "    }",
                            "});",
                            "",
                            "pm.test('List report runs — X-Request-ID round-trip', () => {",
                            "    const rid = pm.response.headers.get('X-Request-ID');",
                            "    pm.expect(rid, 'X-Request-ID header missing').to.be.a('string');",
                            "    pm.expect(rid.length, 'X-Request-ID empty').to.be.greaterThan(0);",
                            "});",
                        ],
                    },
                }
            ],
        },
        # 3. Get one report run (GET /reports/runs/{id})
        {
            "name": "Get report run — GET /reports/runs/{id}",
            "request": {
                "method": "GET",
                "header": [
                    {"key": "Content-Type", "value": "application/json", "type": "text"},
                    {"key": "X-Request-ID", "value": "{{$randomUUID}}", "type": "text"},
                ],
                "url": {
                    "raw": "{{base_url}}/reports/runs/{{report_run_id}}",
                    "host": ["{{base_url}}"],
                    "path": ["reports", "runs", "{{report_run_id}}"],
                },
                "auth": {
                    "type": "bearer",
                    "bearer": [
                        {"key": "token", "value": "{{access_token}}", "type": "string"}
                    ],
                },
            },
            "response": [],
            "event": [
                {
                    "listen": "test",
                    "script": {
                        "type": "text/javascript",
                        "exec": [
                            "pm.test('Get report run — status is in 2xx', () => {",
                            "    pm.expect(pm.response.code, `expected 2xx, got ${pm.response.code}: ${pm.response.text()}`).to.be.within(200, 299);",
                            "});",
                            "",
                            "pm.test('Get report run — response envelope shape', () => {",
                            "    const b = pm.response.json();",
                            "    if (pm.response.code >= 400) {",
                            "        pm.expect(b, 'error envelope missing').to.have.property('error');",
                            "        pm.expect(b.error).to.have.property('code');",
                            "        pm.expect(b.error).to.have.property('message');",
                            "        pm.expect(b.error).to.have.property('request_id');",
                            "        pm.expect(b.error).to.have.property('timestamp');",
                            "    } else {",
                            "        pm.expect(b, 'success envelope missing data').to.have.property('data');",
                            "    }",
                            "});",
                            "",
                            "pm.test('Get report run — X-Request-ID round-trip', () => {",
                            "    const rid = pm.response.headers.get('X-Request-ID');",
                            "    pm.expect(rid, 'X-Request-ID header missing').to.be.a('string');",
                            "    pm.expect(rid.length, 'X-Request-ID empty').to.be.greaterThan(0);",
                            "});",
                        ],
                    },
                }
            ],
        },
        # 4. Export PDF
        {
            "name": "Export report run — GET /reports/runs/{id}/export (PDF)",
            "request": {
                "method": "GET",
                "header": [
                    {"key": "Content-Type", "value": "application/json", "type": "text"},
                    {"key": "X-Request-ID", "value": "{{$randomUUID}}", "type": "text"},
                ],
                "url": {
                    "raw": "{{base_url}}/reports/runs/{{report_run_id}}/export?format=pdf",
                    "host": ["{{base_url}}"],
                    "path": ["reports", "runs", "{{report_run_id}}", "export"],
                    "query": [{"key": "format", "value": "pdf"}],
                },
                "auth": {
                    "type": "bearer",
                    "bearer": [
                        {"key": "token", "value": "{{access_token}}", "type": "string"}
                    ],
                },
            },
            "response": [],
            "event": [
                {
                    "listen": "test",
                    "script": {
                        "type": "text/javascript",
                        "exec": [
                            "pm.test('Export report run (PDF) — status is in 2xx', () => {",
                            "    pm.expect(pm.response.code, `expected 2xx, got ${pm.response.code}: ${pm.response.text()}`).to.be.within(200, 299);",
                            "});",
                            "",
                            "pm.test('Export report run (PDF) — content type is application/pdf', () => {",
                            "    const ct = pm.response.headers.get('Content-Type') || '';",
                            "    pm.expect(ct, `expected application/pdf, got ${ct}`).to.include('application/pdf');",
                            "});",
                            "",
                            "pm.test('Export report run (PDF) — content disposition attachment', () => {",
                            "    const cd = pm.response.headers.get('Content-Disposition') || '';",
                            "    pm.expect(cd, 'Content-Disposition missing').to.include('attachment');",
                            "});",
                        ],
                    },
                }
            ],
        },
        # 5. Export CSV
        {
            "name": "Export report run — GET /reports/runs/{id}/export (CSV)",
            "request": {
                "method": "GET",
                "header": [
                    {"key": "Content-Type", "value": "application/json", "type": "text"},
                    {"key": "X-Request-ID", "value": "{{$randomUUID}}", "type": "text"},
                ],
                "url": {
                    "raw": "{{base_url}}/reports/runs/{{report_run_id}}/export?format=csv",
                    "host": ["{{base_url}}"],
                    "path": ["reports", "runs", "{{report_run_id}}", "export"],
                    "query": [{"key": "format", "value": "csv"}],
                },
                "auth": {
                    "type": "bearer",
                    "bearer": [
                        {"key": "token", "value": "{{access_token}}", "type": "string"}
                    ],
                },
            },
            "response": [],
            "event": [
                {
                    "listen": "test",
                    "script": {
                        "type": "text/javascript",
                        "exec": [
                            "pm.test('Export report run (CSV) — status is in 2xx', () => {",
                            "    pm.expect(pm.response.code, `expected 2xx, got ${pm.response.code}: ${pm.response.text()}`).to.be.within(200, 299);",
                            "});",
                            "",
                            "pm.test('Export report run (CSV) — content type is text/csv', () => {",
                            "    const ct = pm.response.headers.get('Content-Type') || '';",
                            "    pm.expect(ct, `expected text/csv, got ${ct}`).to.include('csv');",
                            "});",
                            "",
                            "pm.test('Export report run (CSV) — content disposition attachment', () => {",
                            "    const cd = pm.response.headers.get('Content-Disposition') || '';",
                            "    pm.expect(cd, 'Content-Disposition missing').to.include('attachment');",
                            "});",
                        ],
                    },
                }
            ],
        },
        # 6. Export XLSX
        {
            "name": "Export report run — GET /reports/runs/{id}/export (XLSX)",
            "request": {
                "method": "GET",
                "header": [
                    {"key": "Content-Type", "value": "application/json", "type": "text"},
                    {"key": "X-Request-ID", "value": "{{$randomUUID}}", "type": "text"},
                ],
                "url": {
                    "raw": "{{base_url}}/reports/runs/{{report_run_id}}/export?format=xlsx",
                    "host": ["{{base_url}}"],
                    "path": ["reports", "runs", "{{report_run_id}}", "export"],
                    "query": [{"key": "format", "value": "xlsx"}],
                },
                "auth": {
                    "type": "bearer",
                    "bearer": [
                        {"key": "token", "value": "{{access_token}}", "type": "string"}
                    ],
                },
            },
            "response": [],
            "event": [
                {
                    "listen": "test",
                    "script": {
                        "type": "text/javascript",
                        "exec": [
                            "pm.test('Export report run (XLSX) — status is in 2xx', () => {",
                            "    pm.expect(pm.response.code, `expected 2xx, got ${pm.response.code}: ${pm.response.text()}`).to.be.within(200, 299);",
                            "});",
                            "",
                            "pm.test('Export report run (XLSX) — content type is spreadsheetml', () => {",
                            "    const ct = pm.response.headers.get('Content-Type') || '';",
                            "    pm.expect(ct, `expected spreadsheetml, got ${ct}`).to.include('spreadsheetml');",
                            "});",
                            "",
                            "pm.test('Export report run (XLSX) — content disposition attachment', () => {",
                            "    const cd = pm.response.headers.get('Content-Disposition') || '';",
                            "    pm.expect(cd, 'Content-Disposition missing').to.include('attachment');",
                            "});",
                        ],
                    },
                }
            ],
        },
    ],
}


def main() -> int:
    if not COLLECTION_PATH.exists():
        print(f"ERROR: {COLLECTION_PATH} not found", file=sys.stderr)
        return 2

    raw = COLLECTION_PATH.read_text(encoding="utf-8")
    collection = json.loads(raw, object_pairs_hook=OrderedDict)

    items = collection.get("item")
    if not isinstance(items, list):
        print("ERROR: collection has no `item` array", file=sys.stderr)
        return 2

    if any(isinstance(s, dict) and s.get("name") == "reports" for s in items):
        print("WARN: `reports` section already exists — skipping")
        return 0

    items.append(REPORTS_SECTION)

    tmp_path = COLLECTION_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(collection, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(COLLECTION_PATH)

    json.loads(COLLECTION_PATH.read_text(encoding="utf-8"))
    print(f"OK: wrote {COLLECTION_PATH} with the new `reports` section")
    print(f"  total sections: {len(items)}")
    print(f"  reports requests: {len(REPORTS_SECTION['item'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
