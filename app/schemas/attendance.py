from datetime import datetime
from typing import List, Literal, Optional

from pydantic import Field, model_validator

from app.schemas.auth import StrictModel

AttemptStatus = Literal[
    "CAPTURED", "PERMISSION_DENIED", "POSITION_UNAVAILABLE", "TIMEOUT", "INSECURE_CONTEXT", "UNSUPPORTED", "ERROR",
]
IdempotencyKey = Field(None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class LocationAttempt(StrictModel):
    """One browser geolocation attempt: a reading, or the reason there is none."""

    status: AttemptStatus
    latitude: Optional[float] = Field(None, ge=-90, le=90, allow_inf_nan=False)
    longitude: Optional[float] = Field(None, ge=-180, le=180, allow_inf_nan=False)
    accuracy_m: Optional[float] = Field(None, gt=0, le=100_000, allow_inf_nan=False)
    captured_at: Optional[datetime] = None
    # 0 when the browser refused before asking (permission already blocked, not HTTPS, no API).
    attempts: Optional[int] = Field(None, ge=0, le=10)
    error_message: Optional[str] = Field(None, max_length=200)

    @model_validator(mode="after")
    def _consistent(self):
        position = (self.latitude, self.longitude, self.accuracy_m)
        if self.status == "CAPTURED":
            if any(v is None for v in position) or self.captured_at is None:
                raise ValueError("a captured reading needs latitude, longitude, accuracy_m and captured_at")
            if self.captured_at.tzinfo is None:
                raise ValueError("captured_at must include a timezone")
            if self.latitude == 0 and self.longitude == 0:
                raise ValueError("0,0 is a placeholder, not a reading")
        elif any(v is not None for v in position):
            raise ValueError("a failed attempt cannot carry coordinates")
        return self


class EventMeta(StrictModel):
    """Optional JSON part of a punch or lunch request (multipart field `meta`)."""

    idempotency_key: Optional[str] = IdempotencyKey
    attendance_id: Optional[int] = None  # the session the page was showing (stale-tab protection)
    location: Optional[LocationAttempt] = None
    client_sent_at: Optional[datetime] = None
    location_consent: bool = False
    exception_reason: Optional[str] = Field(None, min_length=5, max_length=500)
    confirm_close_lunch: bool = False


class SampleSubmission(StrictModel):
    idempotency_key: str = Field(..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    attendance_id: int
    location: LocationAttempt
    client_sent_at: Optional[datetime] = None


class ReviewDecision(StrictModel):
    decision: Literal["APPROVED", "REJECTED", "ACKNOWLEDGED"]
    note: Optional[str] = Field(None, max_length=500)


class BreakWrite(StrictModel):
    started_at: datetime
    ended_at: datetime
    is_paid: Optional[bool] = None


class BreaksReplace(StrictModel):
    breaks: List[BreakWrite] = Field(default_factory=list, max_length=10)
