import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from flask import request as flask_request


class FakeInsertResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class FakeCollection:
    """Minimal in-memory stand-in for a pymongo Collection — just enough of
    the surface area the app actually calls (insert_one, count_documents,
    find/sort/skip/limit, find_one_and_update) to test route logic without
    touching a real database."""

    def __init__(self):
        self.docs = []
        self._next_id = 1

    def insert_one(self, doc):
        stored = dict(doc)
        stored["_id"] = self._next_id
        self._next_id += 1
        self.docs.append(stored)
        return FakeInsertResult(stored["_id"])

    def count_documents(self, _filter=None):
        return len(self.docs)

    def find(self, _filter=None, *_args, **_kwargs):
        return FakeCursor(list(self.docs))

    def find_one_and_update(self, filt, update, upsert=False, return_document=None):
        key = filt.get("_id")
        existing = next((d for d in self.docs if d.get("_id") == key), None)
        if existing is None:
            if not upsert:
                return None
            existing = {"_id": key, "seq": 0}
            self.docs.append(existing)
        for field, amount in update.get("$inc", {}).items():
            existing[field] = existing.get(field, 0) + amount
        return dict(existing)


class FakeCursor(list):
    """Chainable sort/skip/limit that just returns self, matching the calls
    the admin routes make."""
    def sort(self, *args, **kwargs):
        return self

    def skip(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self


@pytest.fixture
def app_module(monkeypatch):
    import app as app_module

    fake_contacts = FakeCollection()
    fake_partnerships = FakeCollection()
    fake_counters = FakeCollection()

    monkeypatch.setattr(app_module, "contact_submissions", fake_contacts)
    monkeypatch.setattr(app_module, "partnership_submissions", fake_partnerships)
    monkeypatch.setattr(app_module, "counters", fake_counters)

    def fake_verify_recaptcha(token, remote_ip=None):
        action = "principals" if flask_request.path.startswith("/principals") else "contact"
        return {"success": True, "score": 0.9, "action": action}

    monkeypatch.setattr(app_module, "verify_recaptcha", fake_verify_recaptcha)
    monkeypatch.setattr(app_module, "send_email", lambda *a, **k: None)

    app_module.app.config.update(TESTING=True)
    app_module.fake_contacts = fake_contacts
    app_module.fake_partnerships = fake_partnerships
    return app_module


@pytest.fixture
def client(app_module):
    with app_module.app.test_client() as c:
        yield c


def get_csrf_token(client, path):
    """GET a form page (which touches the session via {{ csrf_token() }})
    and pull the token back out of the session cookie jar."""
    client.get(path)
    with client.session_transaction() as sess:
        return sess["csrf_token"]
