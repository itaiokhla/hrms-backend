from sqlalchemy import Column, String, Text

from app.models.base import BaseModel


class SystemSetting(BaseModel):
    __tablename__ = "system_settings"

    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=True)