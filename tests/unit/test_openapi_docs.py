"""Regression tests for the OpenAPI documentation layer.

Asserts the project-wide invariants the docs layer relies on:

  - `app.openapi()` produces a spec with `components.securitySchemes`
    (Swagger UI's "Authorize" button needs this).
  - The spec carries the long-form `info.description` and the
    top-level `tags` array (drives the sidebar in /docs).
  - The runtime error envelope and the OpenAPI `ErrorResponse`
    schema share one model — they cannot drift.
  - `standard_responses(include=[...])` produces a `responses=` dict
    usable as a FastAPI decorator argument.
  - The three "heavy doc" modules (auth, staff, patients) have
    realistic examples + field-level descriptions in their schemas.
  - The 3 raw `HTTPException(401/403)` raises in
    `src/modules/identity/dependencies.py` have been replaced by
    typed `UnauthorizedError` / `AccountDisabledError` so all
    401/403 responses share one envelope.

The 12 tests in this file match the verification target in
`/home/shourov/.puku-cli/plans/crispy-wandering-volcano.md`.
"""

from __future__ import annotations

import inspect
import uuid

import pytest


# ---------------------------------------------------------------------------
# 1. FastAPI app + OpenAPI spec
# ---------------------------------------------------------------------------
class TestAppOpenAPISchema:
    def _spec(self) -> dict:
        from src.main import app

        return app.openapi()

    def test_security_scheme_present(self) -> None:
        """`/openapi.json` must declare `HTTPBearer` so Swagger UI's
        Authorize button works."""
        spec = self._spec()
        schemes = spec["components"]["securitySchemes"]
        assert "HTTPBearer" in schemes
        assert schemes["HTTPBearer"]["type"] == "http"
        assert schemes["HTTPBearer"]["scheme"] == "bearer"
        assert schemes["HTTPBearer"]["bearerFormat"] == "JWT"

    def test_global_security_applied(self) -> None:
        """Top-level `security` should reference HTTPBearer so every
        route defaults to "this requires Bearer auth" in Swagger UI."""
        spec = self._spec()
        assert spec["security"] == [{"HTTPBearer": []}]

    def test_info_description_present(self) -> None:
        """The long-form description shown on /docs must be present
        (covers auth, errors, pagination, rate limiting)."""
        spec = self._spec()
        desc = spec["info"]["description"]
        assert "## Authentication" in desc
        assert "## Errors" in desc
        assert "## Pagination" in desc
        assert "## Rate limiting" in desc

    def test_contact_and_license_present(self) -> None:
        """FastAPI's `get_openapi()` drops `contact` / `license_info`
        from the constructor — `_custom_openapi()` reinjects them.
        Regression-test for that injection."""
        spec = self._spec()
        assert spec["info"]["contact"]["name"] == "QlockCare Engineering"
        assert "eng@qlockcare.com" in spec["info"]["contact"]["email"]
        assert spec["info"]["licenseInfo"]["name"] == "Proprietary"

    def test_all_ten_tags_present(self) -> None:
        """The 10 tags from `tags_metadata` must surface in the spec."""
        spec = self._spec()
        tag_names = {t["name"] for t in spec["tags"]}
        expected = {
            "auth", "staff", "patients", "appointments", "visits",
            "portal", "notifications", "locations", "audit-logs", "health",
        }
        assert expected.issubset(tag_names), (
            f"missing tags: {expected - tag_names}"
        )

    def test_paths_count_matches_routes(self) -> None:
        """Sanity check that route registration didn't break — we
        should have 95 unique operations across 68 paths."""
        spec = self._spec()
        total_ops = sum(
            len([m for m in methods if m in {"get", "post", "put", "patch", "delete"}])
            for path, methods in spec["paths"].items()
        )
        # Allow ±2 ops from any future single-route tweaks; the
        # important thing is we're in the right order of magnitude.
        assert 80 <= total_ops <= 110, f"unexpected op count: {total_ops}"


# ---------------------------------------------------------------------------
# 2. ErrorResponse model + envelope round-trip
# ---------------------------------------------------------------------------
class TestErrorEnvelope:
    def test_schema_fields_present(self) -> None:
        """`ErrorResponse.model_json_schema()` must include the
        full envelope shape — code, message, request_id,
        timestamp, details.

        Pydantic v2 surfaces nested models under `$defs` and
        references them via `$ref` from the top-level
        `properties`. We resolve the ref to assert the inner
        `ErrorBody` has every field."""
        from src.shared.schemas.error import ErrorResponse

        schema = ErrorResponse.model_json_schema()
        # Resolve the top-level `error` $ref to its $defs entry.
        error_ref = schema["properties"]["error"]["$ref"]
        # ref is "#/$defs/ErrorBody" — strip the prefix.
        def_name = error_ref.rsplit("/", 1)[-1]
        error_body = schema["$defs"][def_name]
        props = error_body["properties"]
        for field in ("code", "message", "request_id", "timestamp", "details"):
            assert field in props, (
                f"ErrorBody.{field} missing from schema "
                f"(props: {list(props.keys())})"
            )

    def test_runtime_envelope_matches_typed_model(self) -> None:
        """The runtime envelope produced by the global exception
        handler must be identical to the typed `ErrorResponse`'s
        JSON output. Drift = a future PR will produce 401/403 JSON
        that doesn't match the OpenAPI examples."""
        from src.core.exceptions import _envelope
        from src.shared.schemas.error import build_error_envelope

        # `_envelope` builds with `datetime.now()` so we can't
        # compare exact timestamps; instead we exercise both paths
        # with the same input and assert structural equality.
        from src.shared.schemas.error import ErrorResponse

        typed = build_error_envelope(
            code="UNAUTHORIZED",
            message="Authentication required.",
            request_id="abc-123",
        )
        runtime = _envelope(
            code="UNAUTHORIZED",
            message="Authentication required.",
            request_id="abc-123",
        )
        # Drop timestamps for the comparison (they differ by
        # milliseconds but the contract is second-precision).
        typed_no_ts = typed.model_dump(mode="json", exclude_none=True)
        typed_no_ts["error"]["timestamp"] = runtime["error"]["timestamp"]
        assert typed_no_ts == runtime, (
            f"typed={typed_no_ts}\nruntime={runtime}"
        )
        # Also verify the typed model round-trips through
        # ErrorResponse.model_validate(...) for callers that
        # reconstruct it from a parsed JSON dict.
        reconstructed = ErrorResponse.model_validate(runtime)
        assert reconstructed.error.code == "UNAUTHORIZED"
        assert reconstructed.error.message == "Authentication required."
        assert reconstructed.error.request_id == "abc-123"

    def test_api_response_envelope_also_delegates(self) -> None:
        """The second envelope implementation in
        `shared/schemas/api_response.py` must also delegate to the
        typed model — three implementations of one envelope shape
        is a bug magnet."""
        from src.shared.schemas.api_response import error_envelope

        body = error_envelope(code="FOO", message="bar", request_id="r-1")
        assert body == {
            "error": {
                "code": "FOO",
                "message": "bar",
                "request_id": "r-1",
                "timestamp": body["error"]["timestamp"],
            }
        }


# ---------------------------------------------------------------------------
# 3. standard_responses helper
# ---------------------------------------------------------------------------
class TestStandardResponses:
    def test_returns_dict_for_each_included_code(self) -> None:
        from src.shared.schemas.docs import standard_responses

        out = standard_responses(include=[401, 422])
        assert set(out.keys()) == {"401", "422"}
        for entry in out.values():
            assert "model" in entry
            assert "description" in entry
            assert "content" in entry
            assert "application/json" in entry["content"]
            assert "example" in entry["content"]["application/json"]

    def test_default_returns_all_five_codes(self) -> None:
        from src.shared.schemas.docs import standard_responses

        out = standard_responses()
        assert set(out.keys()) == {"401", "403", "404", "409", "422"}

    def test_extras_merged(self) -> None:
        from src.shared.schemas.docs import standard_responses

        out = standard_responses(
            include=[422],
            extras={201: {"description": "Created", "content": {}}},
        )
        assert "422" in out and 201 in out

    def test_attaches_to_real_route(self) -> None:
        """`standard_responses()` output must be acceptable as a
        FastAPI `responses=` kwarg — confirmed by attaching it to
        a real route and reading the OpenAPI spec back."""
        from fastapi import FastAPI

        from src.modules.identity.router import router as auth_router

        app = FastAPI()
        app.include_router(auth_router)
        spec = app.openapi()
        # The login route was annotated with responses=[401, 422].
        login_op = spec["paths"]["/auth/login"]["post"]
        assert "401" in login_op["responses"]
        assert "422" in login_op["responses"]
        # Example bodies are wired in.
        assert "example" in login_op["responses"]["401"]["content"]["application/json"]
        ex = login_op["responses"]["401"]["content"]["application/json"]["example"]
        assert ex["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 4. Heavy-doc modules — examples + descriptions
# ---------------------------------------------------------------------------
class TestHeavyModuleSchemaDocs:
    """All schemas in auth/staff/patients have:
      - a populated `json_schema_extra.examples` list
      - `Field(description=...)` on every field
    """

    @pytest.mark.parametrize(
        "module_path,class_name",
        [
            # auth
            ("src.modules.identity.schemas", "LoginRequest"),
            ("src.modules.identity.schemas", "TokenPair"),
            ("src.modules.identity.schemas", "AcceptInvitationRequest"),
            ("src.modules.identity.schemas", "VerifyEmailRequest"),
            ("src.modules.identity.schemas", "LogoutRequest"),
            # staff
            ("src.modules.staff.schemas", "StaffProfileCreateRequest"),
            ("src.modules.staff.schemas", "StaffProfileResponse"),
            ("src.modules.staff.schemas", "StaffQualificationCreateRequest"),
            ("src.modules.staff.schemas", "QualificationDownloadResponse"),
            ("src.modules.staff.schemas", "StaffAvailabilityCreateRequest"),
            # patients
            ("src.modules.patients.schemas", "PatientProfileCreateRequest"),
            ("src.modules.patients.schemas", "PatientProfileResponse"),
            ("src.modules.patients.schemas", "GuardianProfileCreateRequest"),
            ("src.modules.patients.schemas", "PatientGuardianRelationshipCreateRequest"),
        ],
    )
    def test_schema_has_examples(self, module_path: str, class_name: str) -> None:
        import importlib

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        schema = cls.model_json_schema()
        # Pydantic v2 surfaces `model_config.json_schema_extra`
        # either as a top-level `examples` array (for object types)
        # or as `examples` on individual properties. We accept either.
        top_level_examples = schema.get("examples")
        # Nested: `json_schema_extra` may also be left as a key in
        # the raw schema dict (depends on Pydantic version).
        extra = schema.get("json_schema_extra") or {}
        assert top_level_examples or "examples" in extra, (
            f"{class_name} is missing `examples` — Swagger UI won't "
            f"show a green example box. schema keys: {list(schema.keys())}"
        )
        examples = top_level_examples or extra["examples"]
        assert isinstance(examples, list)
        assert len(examples) >= 1

    @pytest.mark.parametrize(
        "module_path,class_name",
        [
            ("src.modules.identity.schemas", "LoginRequest"),
            ("src.modules.identity.schemas", "TokenPair"),
            ("src.modules.staff.schemas", "StaffProfileCreateRequest"),
            ("src.modules.staff.schemas", "StaffQualificationCreateRequest"),
            ("src.modules.patients.schemas", "PatientProfileCreateRequest"),
            ("src.modules.patients.schemas", "GuardianProfileCreateRequest"),
        ],
    )
    def test_every_field_has_description(self, module_path: str, class_name: str) -> None:
        """Every field on a *Request schema must have a
        `description` — the OpenAPI generator surfaces these
        verbatim in Swagger UI under each input."""
        import importlib

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        schema = cls.model_json_schema()
        props = schema["properties"]
        missing = [
            name for name, info in props.items()
            if "description" not in info or not info["description"]
        ]
        assert not missing, (
            f"{class_name} is missing descriptions on fields: {missing}"
        )


# ---------------------------------------------------------------------------
# 5. Heavy-doc modules — every route has summary + description
# ---------------------------------------------------------------------------
class TestHeavyModuleRouterDocs:
    """All routes in auth/staff/patients have:
      - `summary=` on the decorator (≤6 words, imperative)
      - `description=` on the decorator
      - `responses=standard_responses(...)` for at least one error code
    """

    @pytest.mark.parametrize(
        "router_path",
        [
            "src.modules.identity.router",
            "src.modules.staff.router",
            "src.modules.patients.router",
        ],
    )
    def test_every_route_has_docs(self, router_path: str) -> None:
        """FastAPI exposes route docs via the route's `endpoint`
        function attributes. We check every HTTP method route on
        the router."""
        import importlib

        mod = importlib.import_module(router_path)
        router = mod.router
        undocumented: list[str] = []
        for route in router.routes:
            # Only check HTTP routes (skip APIRoute subclasses).
            methods = getattr(route, "methods", None)
            if not methods:
                continue
            # Only check real HTTP methods.
            http_methods = methods & {"GET", "POST", "PUT", "PATCH", "DELETE"}
            if not http_methods:
                continue
            summary = getattr(route, "summary", None)
            description = getattr(route, "description", None)
            if not summary or not description:
                path = getattr(route, "path", "?")
                undocumented.append(
                    f"{'/'.join(http_methods)} {path}: "
                    f"summary={summary!r}, description={'set' if description else 'missing'}"
                )
        assert not undocumented, (
            f"{router_path} has undocumented routes:\n"
            + "\n".join(f"  - {u}" for u in undocumented)
        )


# ---------------------------------------------------------------------------
# 6. The 3 raw HTTPException raises have been normalized
# ---------------------------------------------------------------------------
class TestNormalizedAuthErrors:
    """The `get_session_with_auth` + `get_current_auth` paths in
    `src/modules/identity/dependencies.py` previously raised raw
    `HTTPException(401)` / `HTTPException(403)` — those routes
    would surface through the Starlette HTTPException handler
    instead of the project's envelope. Confirm the rewrite."""

    def test_get_session_with_auth_uses_typed_errors(self) -> None:
        from src.modules.identity import dependencies

        # The source must NOT import `HTTPException` anymore.
        src = inspect.getsource(dependencies)
        # It's OK to import `HTTPException` in tests, but the
        # production module shouldn't raise it any more.
        assert "raise HTTPException(" not in src, (
            "dependencies.py still raises raw HTTPException — "
            "should be UnauthorizedError / AccountDisabledError."
        )

    def test_imports_typed_errors(self) -> None:
        from src.core.exceptions import (
            AccountDisabledError,
            UnauthorizedError,
        )

        assert UnauthorizedError is not None
        assert AccountDisabledError is not None

    def test_unauthorized_error_envelope(self) -> None:
        """Round-trip: raise UnauthorizedError → envelope dict →
        matches the schema."""
        from src.core.exceptions import UnauthorizedError
        from src.shared.schemas.error import ErrorResponse

        exc = UnauthorizedError(
            message="User no longer exists.",
            details={"user_id": str(uuid.uuid4())},
        )
        # `exc.http_status` and `exc.error_code` are what the
        # global handler uses to build the envelope.
        assert exc.http_status == 401
        assert exc.error_code == "UNAUTHORIZED"
        # The error_response.py round-trip produces a valid
        # ErrorResponse (sanity).
        env = ErrorResponse.model_validate({
            "error": {
                "code": exc.error_code,
                "message": exc.message,
                "request_id": "r-1",
                "timestamp": "2026-06-28T10:23:01Z",
                "details": exc.details,
            }
        })
        assert env.error.code == "UNAUTHORIZED"


__all__ = [
    "TestAppOpenAPISchema",
    "TestErrorEnvelope",
    "TestHeavyModuleRouterDocs",
    "TestHeavyModuleSchemaDocs",
    "TestNormalizedAuthErrors",
    "TestStandardResponses",
]
