from datetime import datetime, timezone
import uuid
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from .settings import get_settings


def now():
    return datetime.now(timezone.utc)


def new_id():
    return str(uuid.uuid4())


def make_engine(url):
    opts = {"pool_pre_ping": True, "hide_parameters": True}
    if url.startswith("sqlite"):
        opts["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in url:
            from sqlalchemy.pool import StaticPool

            opts["poolclass"] = StaticPool
    result = create_engine(url, **opts)
    if url.startswith("sqlite"):

        @event.listens_for(result, "connect")
        def configure_sqlite(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")

    return result


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


def init_db():
    from .models import Base

    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
