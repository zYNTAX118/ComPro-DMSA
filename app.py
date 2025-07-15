import os, logging, base64, json, concurrent.futures, smtplib, requests   # ← add requests
from flask import Flask, request, render_template, flash, redirect, url_for, abort
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from email.mime.text import MIMEText
from dotenv import load_dotenv
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
load_dotenv()

from database import contact_submissions                                 # ← your Mongo collection


# ────────────────────────── Flask + config ─────────────────────────────
app = Flask(__name__)
app.config.update(
    DEBUG=False,
    SECRET_KEY=os.environ.get('SECRET_KEY', os.urandom(32)),
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)
logging.basicConfig(level=logging.INFO)

ADMIN_EMAIL             = os.getenv("ADMIN_EMAIL", "admin@dmsa.co.id")
RECAPTCHA_SITE_KEY      = os.getenv("RECAPTCHA_SITE_KEY")
RECAPTCHA_SECRET_KEY    = os.getenv("RECAPTCHA_SECRET_KEY")
RECAPTCHA_THRESHOLD     = float(os.getenv("RECAPTCHA_THRESHOLD", 0.5))   # feel free to tune

SCOPES = ['https://www.googleapis.com/auth/gmail.send']
executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
gmail_service = None
# ───────────────────────────────────────────────────────────────────────


# ◇────────────────────  reCAPTCHA verifier  ────────────────────◇
def verify_recaptcha(token: str, remote_ip: str | None = None) -> dict:
    """
    Returns Google’s verification JSON.
    Raises ValueError if the call itself fails.
    """
    data = {
        "secret": RECAPTCHA_SECRET_KEY,
        "response": token,
    }
    if remote_ip:
        data["remoteip"] = remote_ip

    r = requests.post("https://www.google.com/recaptcha/api/siteverify", data=data, timeout=5)
    if r.status_code != 200:
        raise ValueError(f"reCAPTCHA HTTP {r.status_code}")
    return r.json()
# ◇───────────────────────────────────────────────────────────────◇


# ───────────────────────  Gmail helpers (unchanged) ───────────────────
def get_credentials():
    raw = os.getenv("TOKEN_JSON")
    if raw:
        info = json.loads(raw)
    else:
        token_path = os.path.join(os.getcwd(), "token.json")
        if not os.path.isfile(token_path):
            raise RuntimeError("No TOKEN_JSON env var and no token.json file found.")
        with open(token_path, "r") as f:
            info = json.load(f)

    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def get_gmail_service():
    global gmail_service
    if gmail_service is None:
        gmail_service = build('gmail', 'v1', credentials=get_credentials())
    return gmail_service


def send_email(to_email, subject, body, attachment=None, filename=None):
    msg = MIMEMultipart()
    msg['To']      = to_email
    msg['From']    = ADMIN_EMAIL
    msg['Subject'] = subject

    msg.attach(MIMEText(body))

    # If there's an attachment, add it
    if attachment and filename:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(attachment.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
        msg.attach(part)

    try:
        service = get_gmail_service()
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId='me', body={'raw': raw}).execute()
        logging.info(f"✉️  Sent via Gmail API to {to_email}")
        return
    except Exception:
        logging.exception("Gmail API failed – falling back to SMTP…")

    smtp_user = os.getenv("SMTP_EMAIL")
    smtp_pass = os.getenv("SMTP_PASS")
    if not smtp_user or not smtp_pass:
        raise RuntimeError("SMTP_EMAIL or SMTP_PASS not set for fallback delivery.")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    logging.info(f"✉️  Sent via SMTP to {to_email}")
# ───────────────────────────────────────────────────────────────────────


# ───────────────────────────  Routes  ──────────────────────────
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
        # 1) reCAPTCHA verification  ─────────────────────────────
        token = request.form.get("g-recaptcha-response")
        if not token:
            flash("reCAPTCHA token missing – please retry.", "error")
            return redirect(url_for('contact'))

        try:
            rc = verify_recaptcha(token, request.remote_addr)
            if not rc.get("success") or rc.get("score", 0) < RECAPTCHA_THRESHOLD or rc.get("action") != "contact":
                logging.warning(f"reCAPTCHA failure: {rc}")
                flash("reCAPTCHA verification failed. Please try again.", "error")
                return redirect(url_for('contact'))
        except Exception as e:
            logging.exception("reCAPTCHA request error")
            flash("Unable to verify reCAPTCHA. Please try later.", "error")
            return redirect(url_for('contact'))
        # ────────────────────────────────────────────────────────

        # 2) Grab the form fields
        name    = request.form.get('name')
        email   = request.form.get('email')
        message = request.form.get('message')
        file = request.files.get("attachment")
        file_data = file.read() if file and file.filename else None
        file_name = file.filename if file and file.filename else None

        if not (name and email and message):
            flash("All fields are required!", "error")
            return redirect(url_for('contact'))

        try:
            # ▸  MongoDB
            res = contact_submissions.insert_one({"name": name, "email": email, "message": message})
            logging.info(f"Inserted contact id={res.inserted_id}")

            # ▸  Notify admin
            send_email(
                ADMIN_EMAIL,
                "New Contact Form Submission",
                f"Name: {name}\nEmail: {email}\n\nMessage:\n{message}",
                attachment=file_data,
                filename=file_name
            )

            # ▸  Auto-reply to user
            send_email(
                email,
                "Thank you for contacting PT. DMSA",
                (
                    f"Dear {name},\n\n"
                    "Thank you for your message. We have received it and will reply soon.\n\n"
                    "Best regards,\nPT. DMSA Team"
                )
            )
            flash("Your message was sent successfully. Thank you!", "success")

        except Exception:
            logging.exception("Error in /contact handler")
            flash("Unexpected error – please try again later.", "error")

        return redirect(url_for('contact'))

    # GET  → render template with the site key
    return render_template('contact.html', site_key=RECAPTCHA_SITE_KEY)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
