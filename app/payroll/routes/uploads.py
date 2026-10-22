"""Upload limits of the payroll screens (F-44): the size, count and type limits the server enforces, so the
browser can refuse an oversized file before sending it. Read-only, no tenant data; any signed-in user."""

from fastapi import APIRouter, Depends

from app.config import settings as app_settings
from app.core.principal import Principal, get_principal
from app.payroll.services import claims, tasks, wage_inputs
from app.payroll.services import compensation as comp_service

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll uploads"])

COMPENSATION_IMPORT_MAX_CHARS = 5_000_000  # routes.compensation.ImportIn.csv


def _mimes(types) -> list:
    return sorted({mime for _magic, _ext, mime in types})


@router.get("/uploads/limits")
def upload_limits(principal: Principal = Depends(get_principal)):
    max_bytes = app_settings.max_upload_size
    return {
        "max_upload_bytes": max_bytes,
        "kinds": {
            "claim_receipt": {"max_bytes": max_bytes, "max_files": claims.MAX_RECEIPTS,
                              "mime_types": _mimes(claims.RECEIPT_TYPES)},
            "task_evidence": {"max_bytes": max_bytes, "max_files": tasks.MAX_EVIDENCE,
                              "mime_types": _mimes(tasks.EVIDENCE_TYPES)},
            "wage_input_import": {"max_bytes": wage_inputs.MAX_IMPORT_BYTES, "max_rows": wage_inputs.MAX_IMPORT_ROWS,
                                  "mime_types": ["text/csv"]},
            "compensation_import": {"max_chars": COMPENSATION_IMPORT_MAX_CHARS,
                                    "max_rows": comp_service.MAX_IMPORT_ROWS, "mime_types": ["text/csv"]},
        },
    }
