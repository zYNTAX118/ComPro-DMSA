import os
import secrets
from flask import Flask, request, render_template, abort
from dotenv import load_dotenv
import logging

load_dotenv()

app=Flask(__name__)
logging.basicConfig(filename='app.log', level=logging.INFO)
app.config['DEBUG']=False
app.config['SECRET_KEY']=os.environ.get('SECRET_KEY', os.urandom(32))
app.config['SESSION_COOKIE_SECURE']=True
app.config['SESSION_COOKIE_HTTPONLY']=True
app.config['SESSION_COOKIE_SAMESITE']='Lax'

api_token=os.environ.get('API_TOKEN', secrets.token_hex(32))

@app.route('/')
def home():
    return render_template('home.html')


@app.route('/about')
def about():
    return render_template('about.html')
@app.route('/mission')
def mission():
    return render_template('mission.html')


@app.route('/products')
def products():
    return render_template('products.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')



if __name__ == '__main__':
    app.run(debug=False)
