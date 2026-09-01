import os, re, hmac, secrets, logging, base64, json, concurrent.futures, smtplib, requests
from flask import Flask, request, render_template, flash, redirect, url_for, abort, session
from googleapiclient.discovery import build
from pymongo import ReturnDocument
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from email.mime.text import MIMEText
from dotenv import load_dotenv
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from io import BytesIO
from werkzeug.exceptions import RequestEntityTooLarge
load_dotenv()
from database import contact_submissions, partnership_submissions, counters   # ← your Mongo collections


# ────────────────────────── Flask + config ─────────────────────────────
app = Flask(__name__)

# Vercel sets VERCEL=1 in its build/runtime environment; absence of it means
# we're running locally over plain HTTP, where a Secure cookie would never
# be sent back by the browser (breaking sessions/CSRF in local dev).
IS_DEPLOYED = bool(os.getenv('VERCEL'))

app.config.update(
    DEBUG=False,
    SECRET_KEY=os.environ.get('SECRET_KEY', os.urandom(32)),
    SESSION_COOKIE_SECURE=IS_DEPLOYED,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,   # 25MB hard cap per request (all attachments combined)
)
logging.basicConfig(level=logging.INFO)

ADMIN_EMAILS            = os.getenv("ADMIN_EMAIL", "").split(",")
RECAPTCHA_SITE_KEY      = os.getenv("RECAPTCHA_SITE_KEY")
RECAPTCHA_SECRET_KEY    = os.getenv("RECAPTCHA_SECRET_KEY")
RECAPTCHA_THRESHOLD     = float(os.getenv("RECAPTCHA_THRESHOLD", 0.5))   # feel free to tune

SCOPES = ['https://www.googleapis.com/auth/gmail.send']
executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
gmail_service = None

ALLOWED_ATTACHMENT_EXTENSIONS = {'.pdf', '.doc', '.docx', '.jpg', '.jpeg', '.png', '.txt', '.xls', '.xlsx'}
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024   # 10MB per individual file
EMAIL_RE = re.compile(r'^[^\s@"<>]+@[^\s@"<>]+\.[^\s@"<>]+$')
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


# ◇──────────────────────  CSRF protection  ────────────────────◇
def get_csrf_token() -> str:
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token


def csrf_valid() -> bool:
    expected = session.get('csrf_token', '')
    submitted = request.form.get('csrf_token', '')
    return bool(expected) and hmac.compare_digest(expected, submitted)


app.jinja_env.globals['csrf_token'] = get_csrf_token
# ◇───────────────────────────────────────────────────────────────◇


def reject(msg: str, status: int, endpoint: str):
    """Shared error response: JSON for XHR submits, flash+redirect otherwise."""
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return {"ok": False, "message": msg}, status
    flash(msg, "error")
    return redirect(url_for(endpoint))


# ◇──────────────────  Input validation helpers  ────────────────◇
def is_valid_email(value: str) -> bool:
    return bool(value) and len(value) <= 254 and bool(EMAIL_RE.match(value))


def sanitize_filename(name: str) -> str:
    """Strip path components and header-breaking characters before the name
    is ever placed inside an email Content-Disposition header."""
    name = os.path.basename(name or "attachment")
    name = re.sub(r'[\r\n"]', '', name).strip()
    return name[:200] or "attachment"


def collect_attachments(files, field_name="attachments"):
    """
    Read + validate uploaded files against an extension allowlist and a
    per-file size cap. Returns (uploaded_files, filenames, error_message).
    On error_message being non-None, the caller should reject the whole
    submission rather than silently dropping the bad file.
    """
    uploaded, filenames = [], []
    for fs in files.getlist(field_name):
        if not fs or not fs.filename:
            continue
        safe_name = sanitize_filename(fs.filename)
        ext = os.path.splitext(safe_name)[1].lower()
        if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
            return [], [], f"File type not allowed: {safe_name}"
        data = fs.read()
        if not data:
            continue
        if len(data) > MAX_ATTACHMENT_SIZE:
            return [], [], f"File too large (max {MAX_ATTACHMENT_SIZE // (1024*1024)}MB): {safe_name}"
        uploaded.append({"filename": safe_name, "data": data, "mimetype": fs.mimetype or "application/octet-stream"})
        filenames.append(safe_name)
    return uploaded, filenames, None
# ◇───────────────────────────────────────────────────────────────◇


# ◇──────────────────  Reference number generator  ────────────────◇
def next_reference_sequence() -> int:
    """
    Yearly counter (000-999, wrapping) used as the #XXX suffix on inquiry
    reference numbers. Keyed by year so it resets to 000 whenever the year
    changes, and is incremented atomically so concurrent submissions never
    collide.
    """
    year = datetime.now(ZoneInfo("Asia/Jakarta")).year
    doc = counters.find_one_and_update(
        {"_id": f"inquiry_reference_{year}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    return (doc["seq"] - 1) % 1000


def make_reference_no(brand: str, seq: int) -> str:
    """Build a human-readable reference like WIKA-260826#000 (BRAND-YYMMDD#XXX)."""
    tag = re.sub(r'[^A-Za-z0-9]', '', (brand or '').split('/')[0].split(',')[0].strip())
    tag = (tag or 'INQ').upper()[:12]
    date_part = datetime.now(ZoneInfo("Asia/Jakarta")).strftime('%y%m%d')
    return f"{tag}-{date_part}#{seq:03d}"
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


# ───────────────────────  Gmail helpers (replace send_email) ───────────────────
def send_email(to_emails, subject, body, attachments=None):
    """
    attachments: optional list of dicts like
      [{"filename":"specs.pdf","data":b"...","mimetype":"application/pdf"}]
    """
    msg = MIMEMultipart()

    if isinstance(to_emails, str):
        to_emails = [to_emails]

    msg['To'] = ", ".join(to_emails)
    msg['From'] = ADMIN_EMAILS[0] if isinstance(ADMIN_EMAILS, list) else ADMIN_EMAILS
    msg['Subject'] = subject
    msg.attach(MIMEText(body, "plain"))

    # Attach any files
    if attachments:
        for att in attachments:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(att["data"])
            encoders.encode_base64(part)
            fname = att.get("filename", "attachment.bin")
            part.add_header('Content-Disposition', f'attachment; filename="{fname}"')
            if att.get("mimetype"):
                part.add_header('Content-Type', att["mimetype"])
            msg.attach(part)

    try:
        service = get_gmail_service()
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId='me', body={'raw': raw}).execute()
        logging.info(f"✉️  Sent via Gmail API to {to_emails}")
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
    logging.info(f"✉️  Sent via SMTP to {to_emails}")

def build_inquiry_pdf(data: dict, message: str, logo_path: str | None = None) -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors

        buf = BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4, title="Inquiry Confirmation", author="PT. DMSA",
            leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30
        )

        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name="Small", fontSize=9, leading=12, textColor=colors.grey))
        styles.add(ParagraphStyle(name="Body", fontSize=10, leading=13))
        styles.add(ParagraphStyle(name="Label", fontSize=10, leading=12, textColor=colors.HexColor("#444")))

        story = []

        if logo_path and os.path.exists(logo_path):
            try:
                story.append(Image(logo_path, width=64, height=64))
                story.append(Spacer(1, 6))
            except Exception:
                pass

        story.append(Paragraph("Inquiry Confirmation", styles["Title"]))
        story.append(Paragraph("Thank you for your inquiry. Below is the summary of your request.", styles["Small"]))
        story.append(Spacer(1, 12))

        contact_rows = []
        def row(label, value):
            contact_rows.append([f"<b>{label}</b>", Paragraph(str(value or "-"), styles["Body"])])

        row("Reference No.", data.get("ticket_id"))
        row("Submitted On", data.get("submitted_on"))
        row("Name", data.get("name"))
        row("Position", data.get("position"))
        row("Company", data.get("company"))
        row("Email", data.get("email"))
        row("Phone / WhatsApp", data.get("phone"))
        row("End User / Site", data.get("end_user"))
        row("Brand / Principal", data.get("brand"))
        row("Category", data.get("category"))
        row("Inquiry Type", data.get("inquiry_type"))
        row("Project / Reference", data.get("project_name"))
        row("Required Delivery", data.get("deadline"))

        contact_tbl = Table(contact_rows, colWidths=[150, 360])
        contact_tbl.setStyle(TableStyle([
            ("BOX", (0,0), (-1,-1), 0.75, colors.HexColor("#e2e8f0")),
            ("INNERGRID", (0,0), (-1,-1), 0.25, colors.HexColor("#e2e8f0")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#f8fafc")),
            ("LEFTPADDING", (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING", (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ]))
        story.append(contact_tbl)
        story.append(Spacer(1, 14))

        story.append(Paragraph("<b>Inquiry Items</b>", styles["Label"]))
        story.append(Spacer(1, 6))

        item_rows = [[
            Paragraph("<b>No</b>", styles["Body"]),
            Paragraph("<b>Qty</b>", styles["Body"]),
            Paragraph("<b>Unit</b>", styles["Body"]),
            Paragraph("<b>Product</b>", styles["Body"]),
            Paragraph("<b>Description / Specs</b>", styles["Body"]),
            Paragraph("<b>Remarks</b>", styles["Body"]),
        ]]

        for idx, item in enumerate(data.get("inquiry_items", []), start=1):
            item_rows.append([
                Paragraph(str(idx), styles["Body"]),
                Paragraph(str(item.get("qty") or "-"), styles["Body"]),
                Paragraph(str(item.get("unit") or "-"), styles["Body"]),
                Paragraph(str(item.get("product") or "-"), styles["Body"]),
                Paragraph(str(item.get("description") or "-").replace("\n", "<br/>"), styles["Body"]),
                Paragraph(str(item.get("remarks") or "-"), styles["Body"]),
            ])

        item_tbl = Table(item_rows, colWidths=[28, 42, 46, 95, 245, 74], repeatRows=1)
        item_tbl.setStyle(TableStyle([
            ("BOX", (0,0), (-1,-1), 0.75, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0,0), (-1,-1), 0.25, colors.HexColor("#cbd5e1")),
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#e2e8f0")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 5),
            ("RIGHTPADDING", (0,0), (-1,-1), 5),
            ("TOPPADDING", (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ]))
        story.append(item_tbl)
        story.append(Spacer(1, 14))

        story.append(Paragraph("<b>Additional Notes</b>", styles["Label"]))
        story.append(Paragraph((message or "-").replace("\n", "<br/>"), styles["Body"]))
        story.append(Spacer(1, 18))

        story.append(Paragraph("PT. Dwi Mandiri Sejahtera Agung", styles["Small"]))
        story.append(Paragraph("Auto-generated for your records.", styles["Small"]))

        doc.build(story)
        return buf.getvalue()
    except Exception:
        logging.info("ReportLab not available; using minimal PDF fallback.")

    # 2) Minimal built-in PDF fallback (single page, basic text)
    try:
        import textwrap
        width, height = 595, 842  # A4 pts
        lines = []

        def _T(x, y, s, font="/F1", size=11):
            s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append(f"BT {font} {size} Tf {x} {y} Td ({s}) Tj ET")

        def _TB(x, y, s):
            _T(x, y, s, font="/F2", size=14)

        _TB(50, height-60, "Inquiry Confirmation")
        y = height - 95

        kv = [
            ("Reference No.", data.get("ticket_id","-")),
            ("Submitted On", data.get("submitted_on","-")),
            ("Name", data.get("name","-")),
            ("Email", data.get("email","-")),
            ("Phone / WhatsApp", data.get("phone","-")),
            ("Company", data.get("company","-")),
            ("Product", data.get("product","-")),
            ("Category", data.get("category","-")),
            ("Inquiry Type", data.get("inquiry_type","-")),
            ("Quantity", data.get("quantity","-")),
            ("Delivery Deadline", data.get("deadline","-")),
        ]
        for k, v in kv:
            _T(50, y, f"{k}:")
            for i, ln in enumerate(textwrap.wrap(str(v), width=75) or ["-"]):
                _T(200, y - 16*i, ln)
            y -= 20 + (16 * max(0, len(textwrap.wrap(str(v), width=75))-1))
            if y < 140:
                break

        _TB(50, y, "Message")
        y -= 20
        for ln in textwrap.wrap(message or "-", width=95):
            _T(50, y, ln)
            y -= 16
            if y < 100:
                break

        _T(50, 70, "PT. Dwi Mandiri Sejahtera Agung — Auto-generated confirmation")

        content = ("\n".join(lines)).encode("latin-1", "ignore")
        objects = []
        def add(o): objects.append(o)

        add("1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
        add("2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
        add("3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>\nendobj\n")
        add("4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
        add("5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>\nendobj\n")
        stream_header = f"6 0 obj\n<< /Length {len(content)} >>\nstream\n".encode("latin-1")
        stream_footer = b"\nendstream\nendobj\n"

        xref_positions = []
        pdf = bytearray()
        pdf += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
        def append_obj(raw):
            xref_positions.append(len(pdf))
            pdf.extend(raw.encode("latin-1"))

        for o in objects:
            append_obj(o)
        xref_positions.append(len(pdf))
        pdf.extend(stream_header)
        pdf.extend(content)
        pdf.extend(stream_footer)

        xref_start = len(pdf)
        count = 6
        xref = ["xref\n0 %d\n" % (count + 1), "0000000000 65535 f \n"]
        for pos in xref_positions:
            xref.append(f"{pos:010d} 00000 n \n")
        pdf.extend("".join(xref).encode("latin-1"))
        trailer = (
            "trailer\n"
            f"<< /Size {count + 1} /Root 1 0 R >>\n"
            "startxref\n"
            f"{xref_start}\n"
            "%%EOF\n"
        ).encode("latin-1")
        pdf.extend(trailer)
        return bytes(pdf)
    except Exception:
        logging.exception("Minimal PDF fallback failed.")
        return b""

def format_inquiry_text(data: dict, message: str, filenames: list[str] | None = None) -> str:
    lines = [
        "New Inquiry",
        "------------",
        f"Reference No. : {data.get('ticket_id','-')}",
        f"Submitted On  : {data.get('submitted_on','-')}",
        f"Name          : {data.get('name','-')}",
        f"Position      : {data.get('position','-')}",
        f"Email         : {data.get('email','-')}",
        f"Company       : {data.get('company','-')}",
        f"Phone         : {data.get('phone','-')}",
        f"End User      : {data.get('end_user','-')}",
        f"Brand         : {data.get('brand','-')}",
        f"Category      : {data.get('category','-')}",
        f"Inquiry Type  : {data.get('inquiry_type','-')}",
        f"Project Ref   : {data.get('project_name','-')}",
        f"Deadline      : {data.get('deadline','-')}",
        "",
        "Inquiry Items:",
    ]

    items = data.get("inquiry_items") or []
    if items:
        for i, item in enumerate(items, start=1):
            lines += [
                f"  Item {i}",
                f"    Qty         : {item.get('qty','-')}",
                f"    Unit        : {item.get('unit','-')}",
                f"    Product     : {item.get('product','-')}",
                f"    Description : {item.get('description','-')}",
                f"    Remarks     : {item.get('remarks','-')}",
                ""
            ]
    else:
        lines.append("  - No item details provided")
        lines.append("")

    lines += [
        "Additional Notes:",
        message or "-",
    ]

    if filenames:
        lines += ["", "Uploaded files:"] + [f" - {n}" for n in filenames]

    return "\n".join(lines)

@app.errorhandler(RequestEntityTooLarge)
def handle_large_upload(e):
    msg = "Upload too large. Please keep total attachments under 25MB."
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return {"ok": False, "message": msg}, 413
    flash(msg, "error")
    return redirect(request.referrer or url_for('home'))


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


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


# ───────────────────────────  Routes  ──────────────────────────
@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        # 1) CSRF check
        if not csrf_valid():
            return reject("Your session expired. Please refresh the page and try again.", 400, 'contact')

        # 2) reCAPTCHA verification
        token = request.form.get("g-recaptcha-response")
        if not token:
            return reject("reCAPTCHA token missing – please retry.", 400, 'contact')

        try:
            rc = verify_recaptcha(token, request.remote_addr)
            if not rc.get("success") or rc.get("score", 0) < RECAPTCHA_THRESHOLD or rc.get("action") != "contact":
                logging.warning(f"reCAPTCHA failure: {rc}")
                return reject("reCAPTCHA verification failed. Please try again.", 400, 'contact')
        except Exception:
            logging.exception("reCAPTCHA request error")
            return reject("Unable to verify reCAPTCHA. Please try later.", 500, 'contact')

        # 3) Grab form fields
        name = request.form.get('name', '').strip()
        position = request.form.get('position', '').strip()
        email = request.form.get('email', '').strip()
        company = request.form.get('company', '').strip()
        phone = request.form.get('phone', '').strip()
        end_user = request.form.get('end_user', '').strip()
        brand = request.form.get('brand', '').strip()
        category = request.form.get('category', '').strip()
        inquiry_type = request.form.get('inquiry_type', 'standard').strip()
        project_name = request.form.get('project_name', '').strip()
        deadline = request.form.get('deadline', '').strip()
        message = request.form.get('message', '').strip()

        raw_items = request.form.get('inquiry_items_json', '[]')
        try:
            inquiry_items = json.loads(raw_items)
            if not isinstance(inquiry_items, list):
                inquiry_items = []
        except Exception:
            inquiry_items = []

        clean_items = []
        for item in inquiry_items:
            if not isinstance(item, dict):
                continue
            row = {
                "qty": str(item.get("qty", "")).strip(),
                "unit": str(item.get("unit", "")).strip(),
                "product": str(item.get("product", "")).strip(),
                "description": str(item.get("description", "")).strip(),
                "remarks": str(item.get("remarks", "")).strip(),
            }
            if any(row.values()):
                clean_items.append(row)

        if not (name and email and company):
            return reject("Name, Company, and Email are required.", 400, 'contact')

        if not is_valid_email(email):
            return reject("Please provide a valid email address.", 400, 'contact')

        if not clean_items:
            return reject("Please provide at least one inquiry item.", 400, 'contact')

        # 4) Files: support both multi 'attachments' and legacy single 'attachment'
        uploaded_files, filenames, file_error = collect_attachments(request.files, "attachments")
        if file_error:
            return reject(file_error, 400, 'contact')

        if request.files.get("attachment"):
            single_files, single_names, single_error = collect_attachments(request.files, "attachment")
            if single_error:
                return reject(single_error, 400, 'contact')
            uploaded_files += single_files
            filenames += single_names

        # 5) Save to Mongo — this is the critical step; if it fails, nothing
        # was recorded so it's safe to tell the user to retry.
        try:
            ticket_id = make_reference_no(brand, next_reference_sequence())
            base_payload = {
                "name": name,
                "position": position,
                "email": email,
                "company": company,
                "phone": phone,
                "end_user": end_user,
                "brand": brand,
                "category": category,
                "inquiry_type": inquiry_type,
                "project_name": project_name,
                "deadline": deadline,
                "message": message,
                "reference_no": ticket_id,
                "inquiry_items": clean_items,
                "created_at": datetime.now(timezone.utc),
            }
            contact_submissions.insert_one(base_payload)
        except Exception:
            logging.exception("Failed to save inquiry to database")
            return reject("Unexpected error – please try again later.", 500, 'contact')

        # From here on the inquiry is safely stored even if email delivery
        # below fails — so a failure here must NOT show the user an error
        # (that would just invite a duplicate resubmission).
        submitted_on = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M %Z")
        payload = dict(base_payload)
        payload["ticket_id"] = ticket_id
        payload["submitted_on"] = submitted_on

        logo = os.path.join(app.root_path, "static", "images", "logo.png")
        pdf_bytes = build_inquiry_pdf(payload, message, logo_path=logo)
        pdf_attachment = [{
            "filename": f"inquiry_{ticket_id}.pdf",
            "data": pdf_bytes,
            "mimetype": "application/pdf"
        }] if pdf_bytes else []

        details_text = format_inquiry_text(payload, message, filenames=filenames)

        admin_subject = f"[DMSA] New Inquiry — Ref #{ticket_id}"
        user_subject  = f"Your Inquiry Received — Ref #{ticket_id}"

        email_failed = False

        try:
            admin_attachments = uploaded_files + pdf_attachment
            send_email(
                ADMIN_EMAILS,
                admin_subject,
                details_text,
                attachments=admin_attachments if admin_attachments else None
            )
        except Exception:
            logging.exception(f"Failed to send admin notification for {ticket_id}")
            email_failed = True

        try:
            send_email(
                email,
                user_subject,
                (
                    f"Dear {name},\n\n"
                    "Thank you for your inquiry. We've received it and will get back to you shortly.\n\n"
                    + details_text +
                    "\n\nBest regards,\nPT. DMSA Team"
                ),
                attachments=pdf_attachment if pdf_attachment else None
            )
        except Exception:
            logging.exception(f"Failed to send confirmation email for {ticket_id}")
            email_failed = True

        msg = f"Your request was sent successfully. Reference No: {ticket_id}"
        if email_failed:
            msg = f"Your inquiry was received and saved (Ref #{ticket_id}). We had trouble sending the confirmation email, but our team already has it."

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return {"ok": True, "message": msg, "reference_no": ticket_id}, 200
        flash(msg, "success")
        return redirect(url_for('contact'))

    # GET
    return render_template('contact.html', site_key=RECAPTCHA_SITE_KEY)


@app.route('/principals', methods=['GET', 'POST'])
def principals():
    if request.method == 'POST':
        if not csrf_valid():
            return reject("Your session expired. Please refresh the page and try again.", 400, 'principals')

        token = request.form.get("g-recaptcha-response")
        if not token:
            return reject("reCAPTCHA token missing – please retry.", 400, 'principals')

        try:
            rc = verify_recaptcha(token, request.remote_addr)
            if not rc.get("success") or rc.get("score", 0) < RECAPTCHA_THRESHOLD or rc.get("action") != "principals":
                logging.warning(f"reCAPTCHA failure on principals page: {rc}")
                return reject("reCAPTCHA verification failed. Please try again.", 400, 'principals')
        except Exception:
            logging.exception("reCAPTCHA request error on principals page")
            return reject("Unable to verify reCAPTCHA. Please try later.", 500, 'principals')

        name = request.form.get('name', '').strip()
        position = request.form.get('position', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()

        company = request.form.get('company', '').strip()
        country = request.form.get('country', '').strip()
        website = request.form.get('website', '').strip()

        product_category = request.form.get('product_category', '').strip()
        industries = request.form.get('industries', '').strip()
        partnership_type = request.form.get('partnership_type', '').strip()
        message = request.form.get('message', '').strip()

        if not (name and email and company and country and partnership_type):
            return reject("Please fill the required fields.", 400, 'principals')

        if not is_valid_email(email):
            return reject("Please provide a valid email address.", 400, 'principals')

        if partnership_type != "principal_manufacturer":
            return reject("Only principal / manufacturer partnership requests are accepted.", 400, 'principals')

        uploaded_files, filenames, file_error = collect_attachments(request.files, "attachments")
        if file_error:
            return reject(file_error, 400, 'principals')

        # Save to Mongo first — this is the record of the request even if
        # the email below never arrives.
        try:
            base_payload = {
                "name": name,
                "position": position,
                "email": email,
                "phone": phone,
                "company": company,
                "country": country,
                "website": website,
                "product_category": product_category,
                "industries": industries,
                "partnership_type": partnership_type,
                "message": message,
                "attachment_filenames": filenames,
                "created_at": datetime.now(timezone.utc),
            }
            partnership_submissions.insert_one(base_payload)
        except Exception:
            logging.exception("Failed to save partnership request to database")
            return reject("Unexpected error – please try again later.", 500, 'principals')

        subject = "[DMSA] New Principal / Manufacturer Partnership Request"

        body = f"""New Partnership Request

            Company: {company}
            Country: {country}
            Website: {website}

            Contact Person:
            Name: {name}
            Position: {position}
            Email: {email}
            Phone: {phone}

            Business Information:
            Main Product Category: {product_category}
            Industries Served: {industries}
            Partnership Type: {partnership_type}

            Additional Information:
            {message or "-"}

            Uploaded Files:
            {chr(10).join('- ' + f for f in filenames) if filenames else '- None'}
            """

        msg = "Your partnership request was sent successfully. Thank you!"
        try:
            send_email(
                ADMIN_EMAILS,
                subject,
                body,
                attachments=uploaded_files if uploaded_files else None
            )
        except Exception:
            logging.exception("Failed to send partnership notification email")
            msg = "Your partnership request was received and saved. We had trouble sending the notification email, but our team already has it."

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return {"ok": True, "message": msg}, 200
        flash(msg, "success")
        return redirect(url_for('principals'))

    return render_template('principals.html', site_key=RECAPTCHA_SITE_KEY)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)






