"""Domain-specific errors with stable, user-safe codes."""


class GateError(Exception):
    """Base error carrying a stable machine-readable code."""

    code = "GATE_ERROR"


class FeedParseError(GateError):
    code = "FEED_PARSE_ERROR"


class ProfileRejectedError(GateError):
    code = "PROFILE_REJECTED"
