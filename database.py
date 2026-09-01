import os
import logging
from pymongo import MongoClient
from pymongo.errors import OperationFailure


# Retrieve the MongoDB connection string from the environment variable or use a default.
MONGO_URI = os.environ.get("MONGO_URI")

# How long submitted form data is kept before MongoDB automatically expires it.
# Applies only to documents that carry a "created_at" field (i.e. new submissions
# going forward) — existing documents inserted before this was added are untouched.
RETENTION_DAYS = int(os.environ.get("CONTACT_RETENTION_DAYS", "730"))  # 2 years


def _ensure_ttl_index(collection, field="created_at", expire_seconds=None, name="ttl_created_at"):
    """
    Create (or update) a TTL index. If the index already exists with a
    different expireAfterSeconds, MongoDB rejects a plain create_index call,
    so fall back to collMod to update it in place.
    """
    try:
        collection.create_index(field, expireAfterSeconds=expire_seconds, name=name)
    except OperationFailure:
        try:
            collection.database.command('collMod', collection.name, index={
                'keyPattern': {field: 1},
                'expireAfterSeconds': expire_seconds,
            })
        except Exception as e:
            logging.getLogger(__name__).warning(f"Could not (re)configure TTL index {name!r}: {e}")


try:
    # Create a MongoClient.
    client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=False)

    # Test the connection to ensure it's valid.
    client.admin.command('ping')

    # Get a reference to the database. Adjust "contact_db" to your desired database name.
    db = client.get_database("contact_db")

    # Get (or create) the collection for contact submissions.
    contact_submissions = db.contact_submissions

    # Get (or create) the collection for principal/manufacturer partnership requests.
    partnership_submissions = db.partnership_submissions

    # Get (or create) the collection used for atomic reference-number counters.
    counters = db.counters

    _ensure_ttl_index(contact_submissions, expire_seconds=RETENTION_DAYS * 86400)
    _ensure_ttl_index(partnership_submissions, expire_seconds=RETENTION_DAYS * 86400)
except Exception as e:
    raise RuntimeError(f"Failed to connect to MongoDB. Error: {e}")
