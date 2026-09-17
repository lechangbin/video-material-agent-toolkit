"""Stable public error codes introduced by the v0.3 contract."""

from enum import StrEnum


class PublicErrorCode(StrEnum):
    FOREIGN_PROXY_REQUIRED = "foreign_proxy_required"
    FOREIGN_PROXY_INVALID = "foreign_proxy_invalid"
    FOREIGN_PROXY_LOST = "foreign_proxy_lost"
    FOREIGN_AUTH_REQUIRED = "foreign_auth_required"
    FOREIGN_PLATFORM_UNSUPPORTED = "foreign_platform_unsupported"
    MANAGED_RUNTIME_UNAVAILABLE = "managed_runtime_unavailable"
    MANAGED_RUNTIME_UPDATE_FAILED = "managed_runtime_update_failed"
    SOURCE_GEOMETRY_INELIGIBLE = "source_geometry_ineligible"
    SOURCE_GEOMETRY_UNKNOWN = "source_geometry_unknown"
    SEARCH_BROWSER_CLOSED = "search_browser_closed"
    EVIDENCE_CORRUPT = "evidence_corrupt"
    SUBAGENT_CAPABILITY_UNAVAILABLE = "subagent_capability_unavailable"
    SUBAGENT_CONTEXT_REQUIRED = "subagent_context_required"
    SUBAGENT_RESULT_INVALID = "subagent_result_invalid"
    SUBAGENT_RETRY_EXHAUSTED = "subagent_retry_exhausted"
    MEDIA_CONFORMANCE_FAILED = "media_conformance_failed"
    MEDIA_CONFORMANCE_CANCELLED = "media_conformance_cancelled"
    ASSEMBLY_SET_INCOMPATIBLE = "assembly_set_incompatible"
    CONCAT_VERIFICATION_FAILED = "concat_verification_failed"
