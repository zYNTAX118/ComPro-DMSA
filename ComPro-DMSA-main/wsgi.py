import logging
from app import app

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    app.run(debug=True)