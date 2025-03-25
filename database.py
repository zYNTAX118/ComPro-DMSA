# database.py
import os
from pymongo import MongoClient

# Retrieve the MongoDB connection string from the environment variable.
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise ValueError("No MONGO_URI environment variable set!")

# Create a MongoClient.
client = MongoClient(MONGO_URI)

# Get a reference to the database. Adjust "myDatabase" to your desired database name.
db = client.get_database("contact_db")

# Get (or create) the collection for contact submissions.
contact_submissions = db.contact_submissions
