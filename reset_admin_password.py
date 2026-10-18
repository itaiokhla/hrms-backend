from app.database.db import SessionLocal
from app.models.user import User
from app.core.security import hash_password

db = SessionLocal()

try:
    user = db.query(User).filter(User.username == "ADMIN").first()

    if not user:
        print("ADMIN user not found")
    else:
        user.password_hash = hash_password("Admin@123")
        db.commit()
        print("Password reset successfully!")
        print("Username: ADMIN")
        print("Password: Admin@123")

finally:
    db.close()