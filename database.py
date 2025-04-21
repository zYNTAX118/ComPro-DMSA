import os
from pymongo import MongoClient


# Retrieve the MongoDB connection string from the environment variable or use a default.
MONGO_URI = os.environ.get("MONGO_URI")

try:
    # Create a MongoClient.
    client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=False, tlsInsecure=False)

    # Test the connection to ensure it's valid.
    client.admin.command('ping')

    # Get a reference to the database. Adjust "contact_db" to your desired database name.
    db = client.get_database("contact_db")

    # Get (or create) the collection for contact submissions.
    contact_submissions = db.contact_submissions
except Exception as e:
    raise RuntimeError(f"Failed to connect to MongoDB. Error: {e}")
