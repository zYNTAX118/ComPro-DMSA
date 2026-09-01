import io
import json

from conftest import get_csrf_token


def contact_form_data(**overrides):
    data = {
        "name": "Izzet",
        "email": "izzet@example.com",
        "company": "PT Example",
        "position": "Engineer",
        "phone": "+62 812",
        "brand": "WIKA",
        "category": "Gauge",
        "inquiry_type": "standard",
        "project_name": "Test Project",
        "deadline": "",
        "message": "Test message",
        "inquiry_items_json": json.dumps([{"description": "Pressure gauge 0-16 kg/cm2"}]),
        "g-recaptcha-response": "fake-token",
    }
    data.update(overrides)
    return data


def principals_form_data(**overrides):
    data = {
        "name": "Vendor Rep",
        "email": "vendor@example.com",
        "phone": "+62 812",
        "company": "Vendor Co",
        "country": "Indonesia",
        "website": "https://example.com",
        "product_category": "Valves",
        "industries": "Oil & Gas",
        "partnership_type": "principal_manufacturer",
        "message": "We'd like to partner.",
        "g-recaptcha-response": "fake-token",
    }
    data.update(overrides)
    return data


# ── CSRF ─────────────────────────────────────────────────────────────
def test_contact_rejects_missing_csrf_token(client):
    resp = client.post(
        "/contact", data=contact_form_data(),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_principals_rejects_missing_csrf_token(client):
    resp = client.post(
        "/principals", data=principals_form_data(),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400


# ── Field validation ─────────────────────────────────────────────────
def test_contact_requires_name_email_company(client):
    token = get_csrf_token(client, "/contact")
    resp = client.post(
        "/contact",
        data=contact_form_data(csrf_token=token, name=""),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400
    assert "required" in resp.get_json()["message"].lower()


def test_contact_rejects_invalid_email(client):
    token = get_csrf_token(client, "/contact")
    resp = client.post(
        "/contact",
        data=contact_form_data(csrf_token=token, email="not-an-email"),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400
    assert "email" in resp.get_json()["message"].lower()


def test_contact_requires_at_least_one_item(client):
    token = get_csrf_token(client, "/contact")
    resp = client.post(
        "/contact",
        data=contact_form_data(csrf_token=token, inquiry_items_json="[]"),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400


# ── File upload validation ──────────────────────────────────────────
def test_contact_rejects_disallowed_file_extension(client):
    token = get_csrf_token(client, "/contact")
    data = contact_form_data(csrf_token=token)
    data["attachments"] = (io.BytesIO(b"MZ..."), "malware.exe")
    resp = client.post(
        "/contact", data=data, content_type="multipart/form-data",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400
    assert "not allowed" in resp.get_json()["message"].lower()


def test_contact_rejects_oversized_file(client, app_module):
    token = get_csrf_token(client, "/contact")
    oversized = b"x" * (app_module.MAX_ATTACHMENT_SIZE + 1)
    data = contact_form_data(csrf_token=token)
    data["attachments"] = (io.BytesIO(oversized), "datasheet.pdf")
    resp = client.post(
        "/contact", data=data, content_type="multipart/form-data",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400
    assert "too large" in resp.get_json()["message"].lower()


# ── Happy paths ──────────────────────────────────────────────────────
def test_contact_happy_path_saves_and_returns_reference(client, app_module):
    token = get_csrf_token(client, "/contact")
    resp = client.post(
        "/contact", data=contact_form_data(csrf_token=token),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["reference_no"].startswith("WIKA-")
    assert len(app_module.fake_contacts.docs) == 1
    assert app_module.fake_contacts.docs[0]["reference_no"] == body["reference_no"]
    assert "created_at" in app_module.fake_contacts.docs[0]


def test_contact_email_failure_still_saves_and_reports_success(client, app_module, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("SMTP is down")
    monkeypatch.setattr(app_module, "send_email", boom)

    token = get_csrf_token(client, "/contact")
    resp = client.post(
        "/contact", data=contact_form_data(csrf_token=token),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    body = resp.get_json()
    # The inquiry must still be considered received even though email failed —
    # otherwise the user gets an error and re-submits, creating a duplicate.
    assert resp.status_code == 200
    assert body["ok"] is True
    assert "trouble sending" in body["message"].lower()
    assert len(app_module.fake_contacts.docs) == 1


def test_contact_db_failure_reports_error_and_saves_nothing(client, app_module, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Mongo is down")
    monkeypatch.setattr(app_module.fake_contacts, "insert_one", boom)

    token = get_csrf_token(client, "/contact")
    resp = client.post(
        "/contact", data=contact_form_data(csrf_token=token),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    body = resp.get_json()
    assert resp.status_code == 500
    assert body["ok"] is False
    assert len(app_module.fake_contacts.docs) == 0


def test_principals_happy_path_saves_to_mongo(client, app_module):
    token = get_csrf_token(client, "/principals")
    resp = client.post(
        "/principals", data=principals_form_data(csrf_token=token),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert len(app_module.fake_partnerships.docs) == 1
    assert app_module.fake_partnerships.docs[0]["company"] == "Vendor Co"


def test_principals_rejects_non_manufacturer_type(client):
    token = get_csrf_token(client, "/principals")
    resp = client.post(
        "/principals",
        data=principals_form_data(csrf_token=token, partnership_type="reseller"),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400
