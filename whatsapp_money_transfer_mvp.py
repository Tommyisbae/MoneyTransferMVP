import os
from flask import Flask, request, jsonify
import pytesseract
from PIL import Image
import re
import requests
from twilio.rest import Client
import psycopg2
import logging
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from urllib.parse import urlparse
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Set up logging to file and console
log_file = '/tmp/app_20250504.log' if os.getenv('RENDER') else os.path.join(os.getcwd(), 'app_20250504.log')
try:
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
except Exception as e:
    print(f"Failed to initialize file handler: {str(e)}")
    file_handler = logging.NullHandler()

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        file_handler,
        logging.StreamHandler()
    ]
)
logging.info("Logging initialized")

# Configuration (using environment variables)
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
WHATSAPP_NUMBER = os.getenv("WHATSAPP_NUMBER", "+14155238886")
DATABASE_URL = os.getenv('DATABASE_URL')

# Initialize Twilio client
try:
    client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
    logging.info("Twilio client initialized successfully")
except Exception as e:
    logging.error(f"Failed to initialize Twilio client: {str(e)}")

# Cache for bank list and codes
BANK_LIST = []
BANK_CODES = {}

# Cache for Paystack account verification
ACCOUNT_CACHE = {}

# Fetch banks from Paystack
def fetch_banks():
    global BANK_LIST, BANK_CODES
    if not BANK_LIST:
        url = "https://api.paystack.co/bank?country=nigeria"
        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
        session.mount('https://', HTTPAdapter(max_retries=retries))
        try:
            logging.info("Fetching banks from Paystack")
            response = session.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            if data['status']:
                BANK_LIST = data['data']
                BANK_CODES = {bank['name']: bank['code'] for bank in BANK_LIST}
                logging.info("Fetched %d banks", len(BANK_LIST))
                logging.debug("Bank names: %s", [bank['name'] for bank in BANK_LIST])
            else:
                logging.error("Failed to fetch banks: %s", data['message'])
        except Exception as e:
            logging.error("Error fetching banks: %s", str(e))
    return BANK_LIST

# Initialize PostgreSQL database
def init_db():
    try:
        url = urlparse(os.getenv("DATABASE_URL"))
        conn = psycopg2.connect(
            database=url.path[1:],
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port
        )
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_number TEXT,
            account_number TEXT,
            bank_name TEXT,
            account_name TEXT,
            amount INTEGER,
            status TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
        conn.close()
        logging.info("Database initialized successfully")
    except Exception as e:
        logging.error(f"Database initialization failed: {str(e)}")

# Extract account details from image
def extract_account_details(image_path):
    logging.info("Starting extract_account_details for %s", image_path)
    try:
        logging.info(f"Checking if image exists: {image_path}")
        if not os.path.exists(image_path):
            logging.error(f"Image file does not exist: {image_path}")
            return None, "Image file not found on disk."
        if os.path.getsize(image_path) == 0:
            logging.error(f"Image file is empty: {image_path}")
            return None, "Image file is empty."
        
        logging.info("Opening image")
        image = Image.open(image_path)
        logging.info("Image opened successfully")
        text = pytesseract.image_to_string(image, config='--oem 3 --psm 6')
        logging.info(f"OCR extracted text: {text}")
        
        account_number_pattern = r'\b\d{10}\b'
        account_number = re.search(account_number_pattern, text)
        
        bank_name = None
        logging.info("Fetching banks for matching")
        banks = fetch_banks()
        text_lower = text.lower()
        for bank in banks:
            bank_clean = bank['name'].lower().replace(" bank", "").replace(" nigeria", "").replace(" digital services limited", "").replace(" (opay)", "").strip()
            logging.debug("Checking bank: %s (cleaned: %s)", bank['name'], bank_clean)
            if bank_clean in text_lower or bank['name'].lower() in text_lower:
                bank_name = bank['name']
                logging.info("Matched bank: %s", bank_name)
                break
        
        if not account_number or not bank_name:
            logging.warning(f"Could not extract account number or bank name. Text: {text}")
            bank_examples = ", ".join([bank['name'] for bank in banks[:3]]) + "..." if banks else "unknown banks"
            return None, f"Sorry, I couldn't read the account number or bank name. Please send a clearer, typed or printed image with a supported bank (e.g., {bank_examples})."
        
        logging.info("Extracted details: account_number=%s, bank_name=%s", account_number.group(), bank_name)
        return {
            "account_number": account_number.group(),
            "bank_name": bank_name
        }, None
    except Exception as e:
        logging.error(f"Error processing image: {str(e)}")
        return None, f"Error reading image: {str(e)}"

# Verify account with Paystack
def verify_account(account_number, bank_name):
    cache_key = f"{account_number}:{bank_name}"
    if cache_key in ACCOUNT_CACHE:
        logging.info(f"Using cached account name for {account_number}")
        return ACCOUNT_CACHE[cache_key], None
    try:
        bank_code = BANK_CODES.get(bank_name)
        if not bank_code:
            logging.warning(f"Bank not supported: {bank_name}")
            banks = fetch_banks()
            bank_examples = ", ".join([bank['name'] for bank in banks[:3]]) + "..." if banks else "unknown banks"
            return None, f"Bank not supported. Supported banks include: {bank_examples}."
        
        url = "https://api.paystack.co/bank/resolve"
        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
        params = {"account_number": account_number, "bank_code": bank_code}
        logging.info(f"Verifying account: {account_number} with bank code: {bank_code}")
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            data = response.json()
            if data["status"]:
                account_name = data["data"]["account_name"]
                ACCOUNT_CACHE[cache_key] = account_name
                logging.info(f"Account verified: {account_name}")
                return account_name, None
            logging.warning("Account verification failed")
            return None, "Account verification failed."
        logging.error(f"Paystack API error, status code: {response.status_code}")
        return None, f"Error contacting bank service: {response.status_code}"
    except Exception as e:
        logging.error(f"Verification error: {str(e)}")
        return None, f"Verification error: {str(e)}"

# Initiate transfer (mock for MVP)
def initiate_transfer(account_number, bank_name, amount, account_name, user_number):
    try:
        url = urlparse(os.getenv("DATABASE_URL"))
        conn = psycopg2.connect(
            database=url.path[1:],
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port
        )
        c = conn.cursor()
        c.execute(
            "INSERT INTO transactions (user_number, account_number, bank_name, account_name, amount, status) VALUES (%s, %s, %s, %s, %s, %s)",
            (user_number, account_number, bank_name, account_name, amount, "Pending")
        )
        conn.commit()
        conn.close()
        logging.info(f"Transfer logged for {account_number}, amount: {amount}")
        return True, None
    except Exception as e:
        logging.error(f"Transfer error: {str(e)}")
        return False, f"Transfer error: {str(e)}"

# Send WhatsApp message
def send_whatsapp_message(to_number, message):
    try:
        client.messages.create(
            from_=f"whatsapp:{WHATSAPP_NUMBER}",
            body=message,
            to=f"whatsapp:{to_number}"
        )
        logging.info(f"Sent message to {to_number}: {message}")
    except Exception as e:
        logging.error(f"Failed to send WhatsApp message: {str(e)}")

# WhatsApp webhook
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    data = request.form
    from_number = data.get("From", "").replace("whatsapp:", "")
    media_url = data.get("MediaUrl0")
    message_body = data.get("Body", "").lower()
    logging.info(f"Received message from {from_number}, Media URL: {media_url}, Body: {message_body}")

    if not hasattr(app, "sessions"):
        app.sessions = {}

    if media_url:
        logging.info(f"Processing media from URL: {media_url}")
        image_path = os.path.join(os.getcwd(), "temp_image.jpg")
        logging.info(f"Saving image to: {image_path}")
        try:
            # Set up retries for image download
            session = requests.Session()
            retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
            session.mount('https://', HTTPAdapter(max_retries=retries))
            
            response = session.get(media_url, auth=(TWILIO_SID, TWILIO_AUTH_TOKEN), timeout=10)
            logging.info(f"Download response status: {response.status_code}")
            if response.status_code != 200:
                logging.error(f"Failed to download image, status code: {response.status_code}, reason: {response.reason}")
                send_whatsapp_message(from_number, f"Error downloading image (status {response.status_code}). Please try again.")
                return jsonify({"status": "error"})
            
            if not response.content:
                logging.error("Downloaded image is empty")
                send_whatsapp_message(from_number, "Downloaded image is empty. Please try again.")
                return jsonify({"status": "error"})
            
            with open(image_path, "wb") as f:
                f.write(response.content)
            logging.info(f"Image saved to {image_path}, size: {os.path.getsize(image_path)} bytes")
            
            details, error = extract_account_details(image_path)
            if error:
                send_whatsapp_message(from_number, error)
                if os.path.exists(image_path):
                    os.remove(image_path)
                    logging.info(f"Removed temporary image: {image_path}")
                return jsonify({"status": "error"})
            
            account_name, error = verify_account(details["account_number"], details["bank_name"])
            if error:
                send_whatsapp_message(from_number, error)
                if os.path.exists(image_path):
                    os.remove(image_path)
                    logging.info(f"Removed temporary image: {image_path}")
                return jsonify({"status": "error"})
            
            app.sessions[from_number] = {
                "details": details,
                "account_name": account_name
            }
            
            confirmation_message = (
                f"Account Details:\n"
                f"Bank: {details['bank_name']}\n"
                f"Account Number: {details['account_number']}\n"
                f"Account Name: {account_name}\n"
                f"Please reply with the amount to transfer (e.g., '5000')"
            )
            send_whatsapp_message(from_number, confirmation_message)
            if os.path.exists(image_path):
                os.remove(image_path)
                logging.info(f"Removed temporary image: {image_path}")
        except Exception as e:
            logging.error(f"Error handling image download: {str(e)}")
            send_whatsapp_message(from_number, f"Error processing image: {str(e)}. Please try again.")
            if os.path.exists(image_path):
                os.remove(image_path)
                logging.info(f"Removed temporary image: {image_path}")
            return jsonify({"status": "error"})
    elif message_body.isdigit() and from_number in app.sessions:
        amount = int(message_body)
        session = app.sessions[from_number]
        success, error = initiate_transfer(
            session["details"]["account_number"],
            session["details"]["bank_name"],
            amount,
            session["account_name"],
            from_number
        )
        if success:
            send_whatsapp_message(from_number, "Transfer request logged! (Note: Actual transfer not implemented in MVP.)")
        else:
            send_whatsapp_message(from_number, error)
        del app.sessions[from_number]
    else:
        send_whatsapp_message(from_number, "Please send a clear image of typed or printed account details (bank name and 10-digit account number). Handwritten text may not be readable.")
    
    return jsonify({"status": "success"})

if __name__ == "__main__":
    init_db()
    fetch_banks()  # Preload banks
    app.run(debug=True)