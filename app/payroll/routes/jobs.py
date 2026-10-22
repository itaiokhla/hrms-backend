"""Background job status for JobProgress (3.9 §4, §6): own jobs or jobs readable through their permission.

GET answers with a strong ETag (SHA-256 of the JSON body) and honours If-None-Match with 304, so pollers
download the job again only when it changed (F-21). Access is checked before any 304: a tag never reveals
that a job the caller cannot read exists.
"""

import hashlib
import json
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import jobs

router = APIRouter(prefix="/api/v1/jobs", tags=["Jobs"])

CACHE_CONTROL = "private, no-cache"


def job_etag(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return '"' + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40] + '"'


def etag_matches(header: Optional[str], etag: str) -> bool:
    """RFC 9110 §13.1.2 weak comparison: `*`, or any listed tag equal to ours with or without W/."""
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


@router.get("/{job_id}")
def get_job(job_id: int, request: Request, principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)):
    body = jsonable_encoder(jobs.serialize(jobs.get_for_principal(db, principal, job_id)))
    etag = job_etag(body)
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL}
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)
