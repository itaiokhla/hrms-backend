from pydantic import BaseModel
from datetime import date
from typing import Optional


class LeaveApplyRequest(BaseModel):
    
    leave_type: str
    start_date: date
    end_date: date
    reason: Optional[str] = None