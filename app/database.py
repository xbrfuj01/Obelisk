import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.schema import CreateColumn

from . import config

DB_PATH = os.path.join(config.DATA_DIR, "app.db")

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def _auto_migrate():
    """Add columns/indexes that exist in the models but not yet in an older DB file on disk."""
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        missing = [c for c in table.columns if c.name not in existing_cols]
        if missing:
            with engine.begin() as conn:
                for column in missing:
                    ddl = str(CreateColumn(column).compile(dialect=engine.dialect))
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)


def init_db():
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _auto_migrate()
