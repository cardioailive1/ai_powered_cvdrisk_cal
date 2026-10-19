"""Create the schema exactly as the pre-Prisma app version did (SQLAlchemy create_all, plain JSON) plus one row,
to test the adoption path of scripts/db_migrate.py.   python tests/make_legacy_db.py <postgresql://...>"""
import datetime as dt
import sys

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class B(DeclarativeBase):
    pass


class User(B):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Assessment(B):
    __tablename__ = "assessments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    calculator: Mapped[str] = mapped_column(String(20))
    inputs: Mapped[dict] = mapped_column(JSON)
    results: Mapped[dict] = mapped_column(JSON)
    model_bundle: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)


e = create_engine("postgresql+psycopg://" + sys.argv[1].split("://", 1)[1])
B.metadata.create_all(e)
with e.begin() as c:
    c.exec_driver_sql("INSERT INTO users (username, password_hash, role, active, created_at) VALUES ('legacy', 'h', 'admin', true, now())")
    c.exec_driver_sql("INSERT INTO assessments (user_id, calculator, inputs, results, model_bundle, created_at) "
                      "VALUES (1, 'ami', '{\"sbp\": 140}', '{}', 'b', now())")
print("legacy schema created")
