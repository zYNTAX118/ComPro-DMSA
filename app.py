# app_optimized.py
import os
import logging
import base64
import json
import concurrent.futures

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
    def get_credentials():
        """
        Load OAuth2 credentials from environment or local file.
        """

        def get_credentials():
            """
            Load Gmail API credentials from environment variable only.
            Avoid fallback to Google Cloud default auth (which fails on Vercel).
            """
            token_json_env = os.getenv("TOKEN_JSON")
            if not token_json_env:
                logging.error("❌ TOKEN_JSON env var not found.")
                raise RuntimeError("TOKEN_JSON environment variable is missing.")

            try:
                creds_info = json.loads(token_json_env)
                creds = Credentials.from_authorized_user_info(creds_info, SCOPES)

                # Optional: refresh token if expired
                if not creds.valid:
                    logging.info("🔁 Token expired, attempting refresh...")
                    if creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                        logging.info("✅ Token refreshed.")
                    else:
                        raise RuntimeError("Refresh token is missing or invalid.")
                return creds
            except Exception as e:
                logging.error(f"❌ Failed to load credentials: {e}")
                raise


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
    Sends an email using the Gmail API.
    """
    service = get_gmail_service()
    message = MIMEText(body)
    message['to'] = to_email
    message['subject'] = subject
    message['From'] = ADMIN_EMAIL
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    sent_message = service.users().messages().send(
        userId='me',
        body={'raw': raw_message}
    ).execute()
    logging.info(f"Email sent to {to_email}. Message ID: {sent_message['id']}")


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
            flash("All fields are required!")
            return redirect(url_for('contact'))
        try:
            # Insert submission into MongoDB.
            submission = {"name": name, "email": email, "message": message}
            result = contact_submissions.insert_one(submission)
            logging.info(f"Inserted submission with id: {result.inserted_id}")

            # Prepare email content.
            admin_subject = "New Contact Form Submission"
            admin_body = f"Name: {name}\nEmail: {email}\n\nMessage:\n{message}"
            user_subject = "Thank you for contacting PT. DMSA"
            user_body = (
                f"Dear {name},\n\n"
                "Thank you for reaching out to us. We have received your message "
                "and will get back to you shortly.\n\nFor further inquiries please contact admin@dmsa.co.id as this is an automated message.\n\n"
                "Best regards,\nPT. DMSA Team"
            )

            # Offload email sending without waiting for completion.
            executor.submit(send_email, ADMIN_EMAIL, admin_subject, admin_body)
            executor.submit(send_email, email, user_subject, user_body)

            flash("Message sent successfully! A confirmation email has been sent.")
        except Exception as e:
            logging.error(f"Error sending message: {e}")
            flash(f"Error sending message: {e}")
        return redirect(url_for('contact'))
    return render_template('contact.html')


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
