import os
from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.testing.plugin.plugin_base import engines

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///contact_data.db")

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class ContactSubmission(Base):
    __tablename__ = 'contact_submission'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    email = Column(String(32), nullable=False)
    message = Column(Text)

Base.metadata.create_all(engine)