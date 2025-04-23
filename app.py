# app_optimized.py
import os
import logging
import base64
import json
import concurrent.futures
import smtplib

from flask import Flask, request, render_template, flash, redirect, url_for
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from email.mime.text import MIMEText
from google.auth.transport.requests import Request
from dotenv import load_dotenv

load_dotenv()

# Initialize Flask and logging
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
app.config['DEBUG'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(32))
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'


ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "senderdmsa@gmail.com")
SCOPES = ['https://www.googleapis.com/auth/gmail.send']

# Global thread pool for sending emails concurrently.
executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
# Cache for the Gmail API service
gmail_service = None


def get_credentials():
    """
       Load credentials from TOKEN_JSON env var (if present) or from a local token.json file.
       Refresh if expired.
       """
    # 1) Try env-var first
    raw = os.getenv("TOKEN_JSON")
    if raw:
        info = json.loads(raw)
    else:
        # fallback to token.json on disk
        token_path = os.path.join(os.getcwd(), "token.json")
        if not os.path.isfile(token_path):
            raise RuntimeError(
                "No TOKEN_JSON env var and no token.json file found."
            )
        with open(token_path, "r") as f:
            info = json.load(f)

    creds = Credentials.from_authorized_user_info(info, SCOPES)
    logging.info(
        f"🔐 creds valid={creds.valid}, expired={creds.expired}, refresh={bool(creds.refresh_token)}"
    )
    if creds.expired:
        if creds.refresh_token:
            logging.info("🔁 refreshing expired token…")
            creds.refresh(Request())
            logging.info("✅ token refreshed")
        else:
            raise RuntimeError(
                "Credentials expired and no refresh token available."
            )
    return creds


def get_gmail_service():
    """
    Returns a cached instance of the Gmail API service.
    """
    global gmail_service
    if gmail_service is None:
        creds = get_credentials()
        gmail_service = build('gmail', 'v1', credentials=creds)
    return gmail_service


def send_email(to_email, subject, body):
    """
        Try Gmail REST API first; on any failure, fall back to SMTP with an app password.
        Any exception here will propagate so that the caller can catch & flash it.
        """
    # build the MIME message
    msg = MIMEText(body)
    msg['To'] = to_email
    msg['From'] = ADMIN_EMAIL
    msg['Subject'] = subject

    # 1) Gmail API path
    try:
        service = get_gmail_service()
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(
            userId='me', body={'raw': raw}
        ).execute()
        logging.info(f"📧 Sent via Gmail API to {to_email}")
        return
    except Exception:
        logging.exception("⚠️  Gmail API send failed, will try SMTP fallback")

    # 2) SMTP fallback
    smtp_user = os.getenv("SMTP_EMAIL")
    smtp_pass = os.getenv("SMTP_PASS")
    if not smtp_user or not smtp_pass:
        raise RuntimeError(
            "SMTP_EMAIL or SMTP_PASS not set for fallback delivery."
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    logging.info(f"📧 Sent via SMTP to {to_email}")


# Import your MongoDB collection
from database import contact_submissions


@app.route('/')
def home():
    return render_template('home.html')


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/products')
def products():
    return render_template('products.html')


@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        message = request.form.get('message')

        if not (name and email and message):
            flash("All fields are required!", "error")
            return redirect(url_for('contact'))

        try:
            # 1) store into MongoDB
            doc = {"name": name, "email": email, "message": message}
            res = contact_submissions.insert_one(doc)
            logging.info(f"➕ Inserted contact id={res.inserted_id}")

            # 2) notify admin
            send_email(
                ADMIN_EMAIL,
                "New Contact Form Submission",
                f"Name: {name}\nEmail: {email}\n\nMessage:\n{message}"
            )

            # 3) thank the user
            send_email(
                email,
                "Thank you for contacting PT. DMSA",
                (
                    f"Dear {name},\n\n"
                    "Thank you for your message. We have received it and will reply soon.\n\n"
                    "Best,\nPT. DMSA Team"
                )
            )

            flash("✅ Your message and emails were sent successfully!", "success")

        except Exception as e:
            logging.exception("❌ Error in /contact handler")
            flash(f"Oops—there was an error: {e}", "error")

        return redirect(url_for('contact'))

    return render_template('contact.html')


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
