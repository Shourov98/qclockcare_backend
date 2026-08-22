"""Reports module — Claude prompt templates.

Two pieces:

  - `SYSTEM_PROMPT` — fixed for all report types. Sets Claude's persona
    (clinical-operations analyst for a home-care agency admin) and the
    hard rules: cite numbers from the snapshot, flag compliance risks,
    refuse to invent data.

  - `build_user_prompt(report_type, params, aggregate)` — assembles the
    per-request user turn: the report type's specific instructions + the
    aggregate JSON snapshot. Claude is told the data availability flag
    so it can phrase gaps honestly rather than hallucinating.

Prompts are deliberately narrow. Claude gets one job per type — write
a 3-5 bullet executive summary + a single risk paragraph. We don't
ask for export formatting, citation footnotes, or anything else; the
narrative is one text blob that the SPA streams into a single panel.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import orjson

from src.modules.reports.schemas import ReportType

# --------------------------------------------------------------------------
# SYSTEM_PROMPT — fixed across all 9 report types + AI_INSIGHTS
# --------------------------------------------------------------------------
SYSTEM_PROMPT: str = (
    "You are a clinical-operations analyst writing for a home-care "
    "agency administrator. The reader is the AGENCY_ADMIN — they "
    "make scheduling, billing, and compliance decisions, not "
    "frontline care. Use plain English; avoid jargon. Numbers are "
    "in US units (dollars, hours, miles). Date format is YYYY-MM-DD.\n"
    "\n"
    "Hard rules:\n"
    "  1. ONLY cite numbers that appear in the data snapshot. NEVER "
    "invent percentages, dollar amounts, visit counts, or credential "
    "counts. If a metric is missing, say so explicitly.\n"
    "  2. Lead with 3-5 short bullets of findings. Then ONE short "
    "paragraph that calls out the most important compliance or "
    "operational risk, if any. Then ONE short paragraph with 2-3 "
    "concrete recommended actions for the admin.\n"
    "  3. When the snapshot includes a `data_gaps` list or a "
    "`_data_availability` of `limited`, acknowledge the gap in your "
    "narrative — the admin needs to know what you CAN'T say.\n"
    "  4. Be concrete. `Three visits failed verification yesterday` "
    "is better than `There were some verification failures`.\n"
    "  5. Do NOT start with a salutation or sign-off. Start with "
    "the first bullet. No preamble.\n"
    "  6. Do NOT use Markdown headers (`#`) — only bullets (`-`) "
    "and plain paragraphs. The SPA renders your output as flowing "
    "text, not as a doc."
)


# --------------------------------------------------------------------------
# Per-type task instructions (the user turn's first half)
# --------------------------------------------------------------------------
_TYPE_TASKS: dict[str, str] = {
    ReportType.VISIT_SUMMARY.value: (
        "Write a Visit Summary Report. Cover: total visits and "
        "completed-visits count, hours billed, the breakdown by "
        "status, and any disputed verifications. Highlight any "
        "disputes and any unusually low completion rates."
    ),
    ReportType.BILLING.value: (
        "Write a Billing Report. Acknowledge that this view is "
        "limited to potential billable units (no claims/denials "
        "data is ingested yet). Recommend next steps the admin "
        "should take to build a complete billing picture."
    ),
    ReportType.COMPLIANCE.value: (
        "Write a Compliance Report. Cover the breakdown of staff "
        "credentials by status. Call out the count expiring "
        "within 30 days and any 'expired but still marked active' "
        "qualifications — those are urgent."
    ),
    ReportType.CLIENT.value: (
        "Write a Client Report. The top clients by visit frequency "
        "and hours are listed. Service Authorization utilization "
        "is NOT available — say so. Identify clients with zero "
        "visits in the window who might need outreach."
    ),
    ReportType.STAFF.value: (
        "Write a Staff Report. Cover per-caregiver hours and visit "
        "counts and their active credential count. Flag any "
        "caregivers with zero active credentials — those are the "
        "compliance risk."
    ),
    ReportType.EVV.value: (
        "Write an EVV (Electronic Visit Verification) Report. Cover "
        "GPS verification rate, address-match rate, manual "
        "overrides, missed clock-ins, and total hours billed. The "
        "manual-override and missed-clock-in counts are the key "
        "risk signals."
    ),
    ReportType.GROUP_HOME.value: (
        "Write a Group Home Report. The data sources (group homes, "
        "ISP compliance, incidents, medication adherence) are NOT "
        "built yet — say so explicitly and recommend what to track "
        "when those modules are built."
    ),
    ReportType.AUDIT_READINESS.value: (
        "Write an Audit Readiness Report. Lead with the count of "
        "uncredentialed staff who completed visits in the window — "
        "that is the most important number. Then cover compliance "
        "gaps by status and the top audit-log action types."
    ),
    ReportType.CUSTOM.value: (
        "Write a Custom Report. Use the visit-summary snapshot as "
        "your data. Mention which columns the user asked to include "
        "(from `requested_columns`) and note any columns that "
        "couldn't be resolved."
    ),
    ReportType.AI_INSIGHTS.value: (
        "Write an AI Insights summary. You have a sample of recent "
        "audit-log actions and the audit-readiness snapshot. "
        "Identify any patterns (e.g. unusual spikes in a particular "
        "action type, repeated failed verifications, compliance "
        "trends) and surface 3 actionable insights."
    ),
}


def build_user_prompt(
    report_type: str,
    params: Mapping[str, Any],
    aggregate: Mapping[str, Any],
) -> str:
    """Assemble the user turn for Claude.

    Three sections, in order:

      1. The task (specific to the report type)
      2. The user's filter context (date window, etc.) — so Claude
         can phrase findings against the right window even when the
         snapshot doesn't repeat it
      3. The aggregate JSON snapshot

    We render the snapshot as compact JSON (`orjson`) so tokens aren't
    wasted on whitespace. Numbers stay as numbers; Claude handles them
    better than quoted strings.
    """
    task = _TYPE_TASKS.get(report_type)
    if task is None:
        # Defensive — should never happen because the router validates
        # the type before reaching here. If it does, fall back to a
        # generic synthesis prompt so Claude at least tries something.
        task = (
            f"Write a report for the unknown report type {report_type!r}. "
            "Use the snapshot below and follow the SYSTEM rules."
        )

    snapshot = orjson.dumps(aggregate).decode("utf-8")
    filter_context = orjson.dumps(dict(params)).decode("utf-8")

    return (
        f"Task:\n{task}\n\n"
        f"User filter parameters:\n{filter_context}\n\n"
        f"Data snapshot (cite only numbers from here):\n```json\n{snapshot}\n```\n"
    )


__all__ = ["SYSTEM_PROMPT", "build_user_prompt"]
