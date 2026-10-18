from sqlalchemy.orm import Session
from app.models.system_settings import SystemSetting


class SettingsService:

    @staticmethod
    def parse_value(value: str):

        if value is None:
            return None

        value = value.strip()

        if value.lower() == "true":
            return True

        if value.lower() == "false":
            return False

        try:
            if "." in value:
                return float(value)
            return int(value)
        except:
            return value

    @staticmethod
    def get_settings(db: Session):

        settings = db.query(SystemSetting).all()

        print("========== SETTINGS ==========")
        for item in settings:
            print(item.key, "=", item.value)

        return {
        item.key: SettingsService.parse_value(item.value)
        for item in settings
    }

    @staticmethod
    def update_settings(db: Session, data: dict):

        for key, value in data.items():

            setting = db.query(SystemSetting).filter(
                SystemSetting.key == key
            ).first()

            if setting:
                setting.value = str(value)

            else:
                db.add(
                    SystemSetting(
                        key=key,
                        value=str(value),
                    )
                )

        db.commit()

        return {
            "message": "Settings Saved Successfully"
        }