"""Reports module — per-report-type data aggregators.

Each aggregator is a pure async function that takes the request's
`AsyncSession`, an `agency_id`, and the user's `params` and returns
a JSON-serializable `dict` snapshot. The snapshot is what the prompt
sends to Claude — and what gets persisted to `report_runs.aggregate_payload`
so PDF/CSV/XLSX exports don't have to re-query.

A few report types don't have source data yet (BILLING claims/denials,
GROUP_HOME / ISP / incidents / medications, Service Authorizations).
Their aggregators return:

    {"_data_availability": "limited",
     "available_metrics": [...],   # what's actually queryable
     "data_gaps": [...],           # what would be needed for a real report
     ...}                          # plus whatever partial data we DO have

…and the corresponding prompt explicitly tells Claude not to invent
numbers. Building the missing tables is out of scope here.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.appointments.models import Appointment
from src.modules.audit_logs.models import AuditLog
from src.modules.patients.models import PatientProfile
from src.modules.staff.models import StaffProfile, StaffQualification
from src.modules.visits.models import ServiceVerification, Visit


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _coerce_date(value: Any) -> date | None:
    """Coerce a `date | str | None` into a `date`.

    The frontend sends ISO strings (`"2026-07-01"`); tests sometimes
    pass `date` objects directly. Anything else returns `None` so the
    caller can fall back to its default.
    """
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _resolve_date_range(params: Mapping[str, Any]) -> tuple[datetime, datetime]:
    """Turn `params['date_from']` / `params['date_to']` into a window.

    Defaults to the last 30 days when either bound is missing — matches
    the dashboard's "Last 30 days" preset and keeps Claude's context
    window bounded.

    The window is `[start_of_day_from, start_of_day_to + 1 day)` — half-
    open, so a report for `date_to=2026-07-31` includes everything that
    happened up to (but not on) 2026-08-01 00:00 UTC. This matches the
    way billing cycles are usually reported.
    """
    today = date.today()
    date_from = _coerce_date(params.get("date_from")) or (today - timedelta(days=30))
    date_to = _coerce_date(params.get("date_to")) or today
    start = datetime.combine(date_from, time.min, tzinfo=UTC)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=UTC)
    return start, end


def _hours_billed(duration_seconds: int | None) -> float:
    """Convert a visit's duration_seconds to decimal hours, rounded to 0.25h."""
    if duration_seconds is None or duration_seconds <= 0:
        return 0.0
    return round(duration_seconds / 3600.0, 2)


# --------------------------------------------------------------------------
# VISIT_SUMMARY
# --------------------------------------------------------------------------
async def aggregate_visit_summary(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Counts and hours-billed for visits inside the date window.

    Drives the "Visit Summary Report" card. Reads from `visits` only —
    appointments are upstream, but the visit row is what actually
    happened (and what gets billed).
    """
    start, end = _resolve_date_range(params)

    base = (
        select(
            func.count(Visit.id).label("total"),
            func.count(Visit.check_out_time).label("completed"),
            func.coalesce(func.sum(Visit.duration_seconds), 0).label("total_seconds"),
        )
        .where(Visit.agency_id == agency_id)
        .where(Visit.check_in_time >= start)
        .where(Visit.check_in_time < end)
    )

    total_row = (await session.execute(base)).one()
    total = int(total_row.total or 0)
    completed = int(total_row.completed or 0)
    hours_billed = _hours_billed(int(total_row.total_seconds or 0))

    # Status breakdown — show how many visits ended in each terminal state.
    status_rows = (
        await session.execute(
            select(Visit.status, func.count(Visit.id))
            .where(Visit.agency_id == agency_id)
            .where(Visit.check_in_time >= start)
            .where(Visit.check_in_time < end)
            .group_by(Visit.status)
        )
    ).all()
    by_status: dict[str, int] = {str(status): count for status, count in status_rows}

    # Disputed verifications — count + sample IDs (capped) for Claude to call out.
    disputed_q = (
        select(func.count(ServiceVerification.id))
        .join(Visit, Visit.id == ServiceVerification.visit_id)
        .where(ServiceVerification.agency_id == agency_id)
        .where(ServiceVerification.status == "DISPUTED")
        .where(ServiceVerification.created_at >= start)
        .where(ServiceVerification.created_at < end)
    )
    disputed_count = int((await session.execute(disputed_q)).scalar() or 0)

    return {
        "_data_availability": "full",
        "window": {
            "from": start.date().isoformat(),
            "to": (end - timedelta(days=1)).date().isoformat(),
        },
        "totals": {
            "visits": total,
            "completed_visits": completed,
            "completion_rate": round(completed / total, 3) if total else 0.0,
            "hours_billed": hours_billed,
        },
        "by_status": by_status,
        "disputed_verifications": disputed_count,
    }


# --------------------------------------------------------------------------
# BILLING
# --------------------------------------------------------------------------
async def aggregate_billing(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Billing claims/denials — partial: we have no claims/denials table yet.

    What we CAN say: how many visits in the window would have generated
    a billing line (i.e. completed visits). Claude is told to phrase the
    report as "potential billable units" and not invent dollar amounts.
    """
    _start, _end = _resolve_date_range(params)
    summary = await aggregate_visit_summary(session, agency_id=agency_id, params=params)

    return {
        "_data_availability": "limited",
        "data_gaps": [
            "claims table not built",
            "denials table not built",
            "payments / adjustments not tracked",
        ],
        "available_metrics": {
            "completed_visits_in_window": summary["totals"]["completed_visits"],
            "hours_billed_in_window": summary["totals"]["hours_billed"],
            "disputed_verifications_in_window": summary["disputed_verifications"],
        },
        "window": summary["window"],
        "narrative_hint": (
            "Treat the 'available_metrics' as potential billable units. "
            "DO NOT invent dollar amounts, denial rates, or payer-mix "
            "percentages — those need a claims pipeline that doesn't exist yet."
        ),
    }


# --------------------------------------------------------------------------
# COMPLIANCE
# --------------------------------------------------------------------------
async def aggregate_compliance(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Staff credentials: total / active / expiring-within-30d / expired.

    This is the reportable slice of compliance today. Background-check
    verifications and policy attestations aren't modeled yet.
    """
    today = date.today()
    in_thirty = today + timedelta(days=30)

    rows = (
        await session.execute(
            select(
                StaffQualification.status,
                func.count(StaffQualification.id),
                func.count(StaffQualification.expires_at).label("with_expiry"),
            )
            .where(StaffQualification.agency_id == agency_id)
            .group_by(StaffQualification.status)
        )
    ).all()

    by_status: dict[str, int] = {str(status): count for status, count, _ in rows}

    # Expiring within 30 days — separated out so the report can flag urgency.
    expiring_q = (
        select(func.count(StaffQualification.id))
        .where(StaffQualification.agency_id == agency_id)
        .where(StaffQualification.status == "ACTIVE")
        .where(StaffQualification.expires_at.is_not(None))
        .where(StaffQualification.expires_at >= today)
        .where(StaffQualification.expires_at <= in_thirty)
    )
    expiring_within_30d = int((await session.execute(expiring_q)).scalar() or 0)

    # Already expired — these are the urgent ones.
    expired_q = (
        select(func.count(StaffQualification.id))
        .where(StaffQualification.agency_id == agency_id)
        .where(StaffQualification.status == "ACTIVE")
        .where(StaffQualification.expires_at.is_not(None))
        .where(StaffQualification.expires_at < today)
    )
    expired_but_active = int((await session.execute(expired_q)).scalar() or 0)

    return {
        "_data_availability": "full",
        "as_of": today.isoformat(),
        "qualifications_by_status": by_status,
        "expiring_within_30d": expiring_within_30d,
        "expired_but_marked_active": expired_but_active,
    }


# --------------------------------------------------------------------------
# CLIENT — visit frequency per patient, no Service Authorization table yet
# --------------------------------------------------------------------------
async def aggregate_client(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Per-client visit counts and hours in the window.

    Sorted by hours descending so the report naturally ranks the
    highest-utilization clients first. The SA (Service Authorization)
    side of the report degrades gracefully — there's no SAs table.
    """
    start, end = _resolve_date_range(params)

    rows = (
        await session.execute(
            select(
                PatientProfile.id,
                PatientProfile.patient_code,
                func.count(Visit.id).label("visit_count"),
                func.coalesce(func.sum(Visit.duration_seconds), 0).label("total_seconds"),
            )
            .select_from(PatientProfile)
            .outerjoin(
                Visit,
                and_(
                    Visit.patient_id == PatientProfile.id,
                    Visit.check_in_time >= start,
                    Visit.check_in_time < end,
                ),
            )
            .where(PatientProfile.agency_id == agency_id)
            .group_by(PatientProfile.id, PatientProfile.patient_code)
            .order_by(func.count(Visit.id).desc())
            .limit(50)
        )
    ).all()

    per_client = [
        {
            "patient_id": str(patient_id),
            "patient_code": patient_code,
            "visits_in_window": int(visit_count),
            "hours_in_window": _hours_billed(int(total_seconds)),
        }
        for patient_id, patient_code, visit_count, total_seconds in rows
    ]

    return {
        "_data_availability": "limited",
        "data_gaps": [
            "service_authorizations table not built — SA utilization unavailable",
        ],
        "window": {
            "from": start.date().isoformat(),
            "to": (end - timedelta(days=1)).date().isoformat(),
        },
        "per_client": per_client,
    }


# --------------------------------------------------------------------------
# STAFF — hours + visits per caregiver + credential status
# --------------------------------------------------------------------------
async def aggregate_staff(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Hours worked + visit count + active credential count per caregiver."""
    start, end = _resolve_date_range(params)

    rows = (
        await session.execute(
            select(
                StaffProfile.id,
                StaffProfile.staff_code,
                func.count(Visit.id).label("visit_count"),
                func.coalesce(func.sum(Visit.duration_seconds), 0).label("total_seconds"),
            )
            .select_from(StaffProfile)
            .outerjoin(
                Visit,
                and_(
                    Visit.staff_id == StaffProfile.id,
                    Visit.check_in_time >= start,
                    Visit.check_in_time < end,
                ),
            )
            .where(StaffProfile.agency_id == agency_id)
            .group_by(StaffProfile.id, StaffProfile.staff_code)
            .order_by(func.count(Visit.id).desc())
            .limit(100)
        )
    ).all()

    per_caregiver = [
        {
            "staff_id": str(staff_id),
            "staff_code": staff_code,
            "visits_in_window": int(visit_count),
            "hours_in_window": _hours_billed(int(total_seconds)),
        }
        for staff_id, staff_code, visit_count, total_seconds in rows
    ]

    # Active credential counts per caregiver — joined on the same rows.
    cred_rows = (
        await session.execute(
            select(
                StaffQualification.staff_id,
                func.count(StaffQualification.id),
            )
            .where(StaffQualification.agency_id == agency_id)
            .where(StaffQualification.status == "ACTIVE")
            .group_by(StaffQualification.staff_id)
        )
    ).all()
    creds_by_staff: dict[str, int] = {str(sid): count for sid, count in cred_rows}
    for entry in per_caregiver:
        entry["active_credentials"] = creds_by_staff.get(entry["staff_id"], 0)

    return {
        "_data_availability": "full",
        "window": {
            "from": start.date().isoformat(),
            "to": (end - timedelta(days=1)).date().isoformat(),
        },
        "per_caregiver": per_caregiver,
    }


# --------------------------------------------------------------------------
# EVV — GPS verification, missed clock-ins, manual overrides
# --------------------------------------------------------------------------
async def aggregate_evv(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Electronic Visit Verification snapshot.

    We don't have a separate EVV table — all of the data lives on
    `visits` (check-in coordinates, address-match flag, duration). The
    "manual override" detector is `check_in_address_match IS NULL` —
    when the staff checked in without GPS verification (offline clock-in
    or location services denied).
    """
    start, end = _resolve_date_range(params)

    base = (
        select(
            func.count(Visit.id).label("total"),
            func.count(Visit.check_in_lat).label("with_gps"),
            func.count(Visit.check_in_address_match).label("with_address_match"),
            func.coalesce(func.sum(Visit.duration_seconds), 0).label("total_seconds"),
        )
        .where(Visit.agency_id == agency_id)
        .where(Visit.check_in_time >= start)
        .where(Visit.check_in_time < end)
    )
    row = (await session.execute(base)).one()
    total = int(row.total or 0)
    with_gps = int(row.with_gps or 0)
    with_address_match = int(row.with_address_match or 0)
    hours = _hours_billed(int(row.total_seconds or 0))

    # Manual overrides — clock-ins without a GPS fix.
    manual_q = (
        select(func.count(Visit.id))
        .where(Visit.agency_id == agency_id)
        .where(Visit.check_in_time >= start)
        .where(Visit.check_in_time < end)
        .where(Visit.check_in_lat.is_(None))
    )
    manual_overrides = int((await session.execute(manual_q)).scalar() or 0)

    # Missed clock-ins — appointments with `checked_in_at IS NULL` after
    # their scheduled_end. This is the "no-show + late cancel" signal.
    missed_q = (
        select(func.count(Appointment.id))
        .where(Appointment.agency_id == agency_id)
        .where(Appointment.scheduled_start >= start)
        .where(Appointment.scheduled_start < end)
        .where(Appointment.checked_in_at.is_(None))
        .where(Appointment.status.notin_(["CANCELLED"]))
    )
    missed_clock_ins = int((await session.execute(missed_q)).scalar() or 0)

    return {
        "_data_availability": "full",
        "window": {
            "from": start.date().isoformat(),
            "to": (end - timedelta(days=1)).date().isoformat(),
        },
        "totals": {
            "visits": total,
            "with_gps_verification": with_gps,
            "gps_verification_rate": round(with_gps / total, 3) if total else 0.0,
            "address_match_rate": round(with_address_match / total, 3) if total else 0.0,
            "manual_overrides": manual_overrides,
            "missed_clock_ins": missed_clock_ins,
            "hours_billed": hours,
        },
    }


# --------------------------------------------------------------------------
# GROUP_HOME — no source data; degrade gracefully
# --------------------------------------------------------------------------
async def aggregate_group_home(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Group Home / ISP / incidents / medications — placeholder.

    We don't have group_home, ISP, incident, or medication tables yet.
    The aggregator returns "data unavailable" plus whatever visit-context
    might be relevant (group home settings are typically visit-driven),
    and the prompt tells Claude to be candid about the gap.
    """
    return {
        "_data_availability": "limited",
        "data_gaps": [
            "group_homes table not built",
            "ISP (Individual Service Plan) table not built",
            "incidents table not built",
            "medications / medication_administration table not built",
        ],
        "narrative_hint": (
            "Tell the agency admin that this report cannot be fully "
            "populated without the Group Home / ISP / incidents / "
            "medication-administration modules. Recommend only on the "
            "data we DO have. Do NOT invent compliance percentages."
        ),
    }


# --------------------------------------------------------------------------
# AUDIT_READINESS — derive from `audit_logs` + compliance gaps
# --------------------------------------------------------------------------
async def aggregate_audit_readiness(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Audit-trail coverage + outstanding compliance gaps.

    Useful as a DHS / Optum prep summary — auditors usually ask:
      (1) Did you log the action?
      (2) Is the credential on file + active?
    """
    start, end = _resolve_date_range(params)

    # Total audit events in window — by action type, top 10.
    action_rows = (
        await session.execute(
            select(AuditLog.action, func.count(AuditLog.id))
            .where(AuditLog.agency_id == agency_id)
            .where(AuditLog.created_at >= start)
            .where(AuditLog.created_at < end)
            .group_by(AuditLog.action)
            .order_by(func.count(AuditLog.id).desc())
            .limit(10)
        )
    ).all()
    by_action: dict[str, int] = {str(action): count for action, count in action_rows}

    # Outstanding compliance gaps — staff with zero ACTIVE credentials
    # who completed at least one visit in the window. This is a hard
    # "uh-oh" signal: they were working but not credentialed.
    uncred_q = (
        select(func.count(func.distinct(StaffProfile.id)))
        .select_from(StaffProfile)
        .join(Visit, Visit.staff_id == StaffProfile.id)
        .where(StaffProfile.agency_id == agency_id)
        .where(Visit.check_in_time >= start)
        .where(Visit.check_in_time < end)
        .where(
            ~select(StaffQualification.id)
            .where(StaffQualification.staff_id == StaffProfile.id)
            .where(StaffQualification.status == "ACTIVE")
            .exists()
        )
    )
    uncredentialed_visiting_staff = int(
        (await session.execute(uncred_q)).scalar() or 0
    )

    # Compliance snapshot — same data the COMPLIANCE report uses.
    compliance = await aggregate_compliance(session, agency_id=agency_id, params=params)

    return {
        "_data_availability": "full",
        "window": {
            "from": start.date().isoformat(),
            "to": (end - timedelta(days=1)).date().isoformat(),
        },
        "audit_log_top_actions": by_action,
        "uncredentialed_visiting_staff_in_window": uncredentialed_visiting_staff,
        "compliance_snapshot": compliance,
        "narrative_hint": (
            "Lead with the uncredentialed-visiting-staff number — that "
            "is the single most important number for an audit prep. "
            "Then walk through compliance gaps by status."
        ),
    }


# --------------------------------------------------------------------------
# CUSTOM — same as VISIT_SUMMARY but column-filtered for the export
# --------------------------------------------------------------------------
async def aggregate_custom(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Custom report — currently delegates to visit-summary shape.

    The frontend's column-picker filters the export side (`get_artifact`),
    not the aggregator — keeps the prompt simple. Future enhancement:
    dispatch on `params['columns']` to a specialised aggregator.
    """
    base = await aggregate_visit_summary(session, agency_id=agency_id, params=params)
    base["_data_availability"] = "full"
    base["_source"] = "visit_summary"
    requested = params.get("columns") or []
    base["requested_columns"] = [
        col for col in requested if col != ""
    ]  # unknown columns dropped by exporter
    return base


# --------------------------------------------------------------------------
# AI_INSIGHTS — synthesis across all reports; reads audit_logs only
# --------------------------------------------------------------------------
async def aggregate_ai_insights(
    session: AsyncSession,
    *,
    agency_id: uuid.UUID,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Cross-cutting signals for the AI Insights banner.

    Pulls the most recent audit_log actions + the audit-readiness
    snapshot — Claude uses this to identify patterns across visits,
    compliance, and access events.
    """
    start, end = _resolve_date_range(params)

    recent_q = (
        select(AuditLog.action, AuditLog.entity_type, AuditLog.created_at)
        .where(AuditLog.agency_id == agency_id)
        .where(AuditLog.created_at >= start)
        .where(AuditLog.created_at < end)
        .order_by(AuditLog.created_at.desc())
        .limit(100)
    )
    recent = (await session.execute(recent_q)).all()
    recent_actions = [
        {
            "action": str(action),
            "entity_type": entity_type,
            "at": when.isoformat() if when else None,
        }
        for action, entity_type, when in recent
    ]

    audit_readiness = await aggregate_audit_readiness(
        session, agency_id=agency_id, params=params
    )

    return {
        "_data_availability": "limited",
        "data_gaps": [
            "AI Insights currently reads audit_logs + compliance only — "
            "not all 9 reports' underlying tables",
        ],
        "window": {
            "from": start.date().isoformat(),
            "to": (end - timedelta(days=1)).date().isoformat(),
        },
        "recent_actions_sample": recent_actions,
        "audit_readiness_snapshot": audit_readiness,
    }


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
AGGREGATORS: dict[str, Any] = {
    "visit_summary": aggregate_visit_summary,
    "billing": aggregate_billing,
    "compliance": aggregate_compliance,
    "client": aggregate_client,
    "staff": aggregate_staff,
    "evv": aggregate_evv,
    "group_home": aggregate_group_home,
    "audit_readiness": aggregate_audit_readiness,
    "custom": aggregate_custom,
    "ai_insights": aggregate_ai_insights,
}


def get_aggregator(report_type: str) -> Any:
    """Look up an aggregator by report-type string.

    Raises `KeyError` (not `ValueError`) so the router's `try/except KeyError`
    block can catch it cleanly — `ValueError` is too broad and could swallow
    unrelated bugs.
    """
    try:
        return AGGREGATORS[report_type]
    except KeyError:
        raise KeyError(
            f"Unknown report type: {report_type!r}. "
            f"Valid: {sorted(AGGREGATORS)}"
        ) from None


__all__ = [
    "AGGREGATORS",
    "aggregate_ai_insights",
    "aggregate_audit_readiness",
    "aggregate_billing",
    "aggregate_client",
    "aggregate_compliance",
    "aggregate_custom",
    "aggregate_evv",
    "aggregate_group_home",
    "aggregate_staff",
    "aggregate_visit_summary",
    "get_aggregator",
]
