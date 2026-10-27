from typing import Any, Mapping, Optional

from fastapi import HTTPException, status

# Keys an error body may carry beside detail and code (LH IG-06, V3-091).
EXTRA_KEYS = frozenset({"issues", "details", "allowed_actions"})


class AppError(HTTPException):
    """HTTP error carrying a stable machine-readable code for the frontend.

    ``extra`` adds structured fields to the JSON body beside ``detail`` and ``code`` (only EXTRA_KEYS).
    """

    def __init__(self, status_code: int, detail: str, code: Optional[str] = None, headers=None,
                 extra: Optional[Mapping[str, Any]] = None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        unknown = set(extra or ()) - EXTRA_KEYS
        if unknown:
            raise ValueError(f"AppError extra keys not allowed: {sorted(unknown)}")
        self.extra = dict(extra or {})


def not_found(what: str = "Resource") -> AppError:
    # Used for missing records *and* records outside the caller's scope, so
    # responses never reveal whether another tenant's record exists.
    return AppError(status.HTTP_404_NOT_FOUND, f"{what} not found", "NOT_FOUND")


def forbidden(detail: str = "You don't have permission to perform this action.") -> AppError:
    return AppError(status.HTTP_403_FORBIDDEN, detail, "FORBIDDEN")


def bad_request(detail: str, code: str = "BAD_REQUEST") -> AppError:
    return AppError(status.HTTP_400_BAD_REQUEST, detail, code)


def conflict(detail: str, code: str = "CONFLICT") -> AppError:
    return AppError(status.HTTP_409_CONFLICT, detail, code)


def unauthorized(detail: str = "Not authenticated", code: str = "UNAUTHENTICATED") -> AppError:
    return AppError(status.HTTP_401_UNAUTHORIZED, detail, code)
