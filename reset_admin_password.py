from app.database.db import SessionLocal
from app.models.user import User
from app.core.security import hash_password

db = SessionLocal()

try:
    user = db.query(User).filter(User.username.in_(["admin", "ADMIN"])).first()

    if not user:
        user = User(
            username="admin",
            email="admin@hrms.com",
            password_hash=hash_password("Admin@123"),
            role="admin",
            is_active=True,
        )
        db.add(user)
        db.commit()
        print("Admin user created successfully!")
        print("Username: admin")
        print("Password: Admin@123")
    else:
        user.password_hash = hash_password("Admin@123")
        db.commit()
        print("Password reset successfully!")
        print(f"Username: {user.username}")
        print("Password: Admin@123")

finally:
    db.close()