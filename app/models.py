import uuid
from datetime import datetime

from sqlalchemy import Column, String, Float, DateTime, Integer, Text

from .database import Base


def gen_id() -> str:
    return uuid.uuid4().hex


class Download(Base):
    __tablename__ = "downloads"

    id = Column(String, primary_key=True, default=gen_id)
    url = Column(Text, nullable=False)
    source = Column(String, nullable=True)

    mode = Column(String, nullable=False)  # video | video_only | audio
    quality = Column(String, nullable=True)
    container = Column(String, nullable=True)
    subtitle_lang = Column(String, nullable=True)
    premiere_compat = Column(Integer, default=0)

    status = Column(String, default="queued")  # queued, downloading, finished, error, expired, deleted
    progress = Column(Float, default=0.0)

    title = Column(Text, nullable=True)
    filepath = Column(Text, nullable=True)
    filesize = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)

    client_ip = Column(String, nullable=True)
    client_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_id)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
