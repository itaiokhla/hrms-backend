from pydantic import BaseModel


class SettingItem(BaseModel):
    key: str
    value: str | None = None


class SettingUpdate(BaseModel):
    settings: list[SettingItem]