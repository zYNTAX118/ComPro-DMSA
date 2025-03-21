# app.py
import os
import secrets
import logging
import base64
import json

from flask import Flask, request, render_template, flash, redirect, url_for
from dotenv import load_dotenv

# Google API imports
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from email.mime.text import MIMEText

# Load .env (for local dev). On Vercel, environment variables are provided automatically.
load_dotenv()

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)

# Flask config
app.config['DEBUG'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(32))
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "senderdmsa@gmail.com")

SCOPES = ['https://www.googleapis.com/auth/gmail.send']

def get_credentials():
    """
    Loads OAuth2 credentials from an environment variable or from a local token.json file.
    """
    token_json_env = os.getenv("TOKEN_JSON")  # set this in production (Vercel)
    if token_json_env:
        # Parse from environment variable
        creds_info = json.loads(token_json_env)
        creds = Credentials.from_authorized_user_info(creds_info, SCOPES)
        if not creds or not creds.valid:
            # Attempt refresh if token is expired but we have a refresh token
            if creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
        return creds
    else:
        # Fallback to local file (development)
        if os.path.exists('token.json'):
            return Credentials.from_authorized_user_file('token.json', SCOPES)
        else:
            raise RuntimeError("No credentials found. Run oauth.py locally or set TOKEN_JSON env.")

def send_email(to_email, subject, body):
    """
    Sends an email via the Gmail API using OAuth2 credentials.
    """
    creds = get_credentials()
    service = build('gmail', 'v1', credentials=creds)

    message = MIMEText(body)
    message['to'] = to_email
    message['subject'] = subject
    message['From'] = "senderdmsa@gmail.com"

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

    sent_message = service.users().messages().send(
        userId='me',
        body={'raw': raw_message}
    ).execute()
    logging.info(f"Email sent to {to_email}. Message ID: {sent_message['id']}")

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
            # Admin notification
            admin_subject = "New Contact Form Submission"
            admin_body = f"Name: {name}\nEmail: {email}\n\nMessage:\n{message}"
            send_email(ADMIN_EMAIL, admin_subject, admin_body)

            # User confirmation
            user_subject = "Thank you for contacting PT. DMSA"
            user_body = (
                f"Dear {name},\n\n"
                "Thank you for reaching out to us. We have received your message "
                "and will get back to you shortly.\n\nFor further inquiries please contact admin@dmsa.co.id since this is an automated message"
                "\n\nBest regards,\nPT. DMSA Team"
            )
            send_email(email, user_subject, user_body)

            flash("Message sent successfully! A confirmation email has been sent.")
        except Exception as e:
            flash(f"Error sending message: {e}")

        return redirect(url_for('contact'))

    return render_template('contact.html')

if __name__ == '__main__':
    # For local testing only
    app.run(debug=False, host='0.0.0.0', port=5000)
