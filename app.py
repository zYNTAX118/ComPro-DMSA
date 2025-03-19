import os
import secrets
from flask import Flask, request, render_template, abort, flash, redirect, url_for
from dotenv import load_dotenv
import logging
import smtplib

load_dotenv()

app=Flask(__name__)
logging.basicConfig(level=logging.INFO)
app.config['DEBUG']=False
app.config['SECRET_KEY']=os.environ.get('SECRET_KEY', os.urandom(32))
app.config['SESSION_COOKIE_SECURE']=True
app.config['SESSION_COOKIE_HTTPONLY']=True
app.config['SESSION_COOKIE_SAMESITE']='Lax'

api_token=os.environ.get('API_TOKEN', secrets.token_hex(32))

MAIL_SERVER = os.getenv("MAIL_SERVER")
MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
MAIL_USERNAME = os.getenv("MAIL_USERNAME")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")

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
        # Retrieve form data
        name = request.form.get('name')
        email = request.form.get('email')
        message = request.form.get('message')

        if not (name and email and message):
            flash("All fields are required!")
            return redirect(url_for('contact'))

        try:
            # Email notification to admin/company
            admin_subject = "New Contact Form Submission"
            admin_body = f"Name: {name}\nEmail: {email}\n\nMessage:\n{message}"
            send_email(MAIL_USERNAME, admin_subject, admin_body)

            # Confirmation email to the user
            user_subject = "Thank you for contacting PT. DMSA"
            user_body = (
                f"Dear {name},\n\n"
                "Thank you for reaching out to us. We have received your message and "
                "will get back to you shortly.\n\nBest regards,\nPT. DMSA Team"
            )
            send_email(email, user_subject, user_body)

            flash("Message sent successfully! A confirmation email has been sent to you.")
        except Exception as e:
            flash(f"Error sending message: {e}")

        return redirect(url_for('contact'))

    return render_template('contact.html')


def send_email(to_email, subject, body):
    from_email = MAIL_USERNAME
    message = f"Subject: {subject}\n\n{body}"

    with smtplib.SMTP(MAIL_SERVER, MAIL_PORT) as server:
        server.starttls()
        server.login(MAIL_USERNAME, MAIL_PASSWORD)
        server.sendmail(from_email, to_email, message)
    return render_template('contact.html')



if __name__ == '__main__':
    app.run(debug=False)
