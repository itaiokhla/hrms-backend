from typing import Optional

from fastapi import HTTPException, status


class AppError(HTTPException):
    """HTTP error carrying a stable machine-readable code for the frontend."""

    def __init__(self, status_code: int, detail: str, code: Optional[str] = None, headers=None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


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
