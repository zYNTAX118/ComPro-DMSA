import os
from pymongo import MongoClient
from sqlalchemy import Column, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class ContactSubmission(Base):
    __tablename__ = "contact_submissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)


# Retrieve the MongoDB connection string from the environment variable or use a default.
MONGO_URI = os.environ.get("MONGO_URI")

try:
    # Create a MongoClient.
    client = MongoClient(MONGO_URI)

    # Test the connection to ensure it's valid.
    client.admin.command('ping')

    # Get a reference to the database. Adjust "contact_db" to your desired database name.
    db = client.get_database("contact_db")

    # Get (or create) the collection for contact submissions.
    contact_submissions = db.contact_submissions
except Exception as e:
    raise RuntimeError(f"Failed to connect to MongoDB. Error: {e}")
