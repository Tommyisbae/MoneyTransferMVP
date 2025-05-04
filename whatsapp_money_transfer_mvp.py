import os
from flask import Flask, request, jsonify
import pytesseract
from PIL import Image, ImageEnhance
import re
import requests
from twilio.rest import Client
import psycopg2
import logging
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from urllib.parse import urlparse
from dotenv import load_dotenv
import uuid
from datetime import datetime

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
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            phone_number TEXT PRIMARY KEY,
            balance INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_number TEXT,
            account_number TEXT,
            bank_name TEXT,
            account_name TEXT,
            amount INTEGER,
            fee INTEGER,
            type TEXT,  -- 'fund', 'transfer', 'failed'
            status TEXT,  -- 'Pending', 'Success', 'Failed'
            transfer_code TEXT,
            reference TEXT,
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
        image = ImageEnhance.Contrast(image).enhance(2.0)
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

# Get user balance
def get_user_balance(phone_number):
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
        c.execute("SELECT balance FROM users WHERE phone_number = %s", (phone_number,))
        result = c.fetchone()
        conn.close()
        if result:
            return result[0]
        else:
            c = conn.cursor()
            c.execute("INSERT INTO users (phone_number, balance) VALUES (%s, %s)", (phone_number, 0))
            conn.commit()
            conn.close()
            return 0
    except Exception as e:
        logging.error(f"Error fetching balance: {str(e)}")
        return None

# Get transaction history
def get_transaction_history(phone_number):
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
            "SELECT type, amount, fee, status, transfer_code, reference, timestamp FROM transactions WHERE user_number = %s ORDER BY timestamp DESC LIMIT 5",
            (phone_number,)
        )
        transactions = c.fetchall()
        conn.close()
        history = []
        for tx in transactions:
            tx_type, amount, fee, status, transfer_code, reference, timestamp = tx
            history.append(
                f"{timestamp.strftime('%Y-%m-%d %H:%M')}: {tx_type.capitalize()} ₦{amount} (Fee: ₦{fee or 0}, Status: {status}, ID: {transfer_code or reference or 'N/A'})"
            )
        return history
    except Exception as e:
        logging.error(f"Error fetching transaction history: {str(e)}")
        return None

# Reconcile balances
def reconcile_balances():
    try:
        # Get Paystack balance
        url = "https://api.paystack.co/balance"
        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            logging.error(f"Failed to fetch Paystack balance: {response.text}")
            return False, "Failed to fetch Paystack balance"
        paystack_balance = response.json()['data'][0]['balance'] // 100  # Convert from kobo
        
        # Get total user balances
        url = urlparse(os.getenv("DATABASE_URL"))
        conn = psycopg2.connect(
            database=url.path[1:],
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port
        )
        c = conn.cursor()
        c.execute("SELECT SUM(balance) FROM users")
        total_user_balance = c.fetchone()[0] or 0
        conn.close()
        
        logging.info(f"Paystack balance: ₦{paystack_balance}, Total user balances: ₦{total_user_balance}")
        if abs(paystack_balance - total_user_balance) > 100:  # Allow small discrepancy for fees
            logging.warning(f"Balance mismatch: Paystack ₦{paystack_balance} vs Users ₦{total_user_balance}")
            return False, f"Balance mismatch: Paystack ₦{paystack_balance} vs Users ₦{total_user_balance}"
        return True, "Balances reconciled"
    except Exception as e:
        logging.error(f"Reconciliation error: {str(e)}")
        return False, f"Reconciliation error: {str(e)}"

# Initiate transfer with Paystack
def initiate_transfer(account_number, bank_name, amount, account_name, user_number):
    try:
        # Check user balance
        balance = get_user_balance(user_number)
        if balance is None or balance < amount:
            logging.warning(f"Insufficient balance for {user_number}: {balance} < {amount}")
            return False, "Insufficient balance. Please fund your wallet with /fund <amount>."
        
        # Create transfer recipient
        recipient_url = "https://api.paystack.co/transferrecipient"
        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
        recipient_data = {
            "type": "nuban",
            "name": account_name,
            "account_number": account_number,
            "bank_code": BANK_CODES.get(bank_name),
            "currency": "NGN"
        }
        logging.info(f"Creating transfer recipient for {account_number}")
        response = requests.post(recipient_url, headers=headers, json=recipient_data)
        if response.status_code != 201:
            logging.error(f"Failed to create recipient: {response.text}")
            return False, f"Failed to create recipient: {response.status_code}"
        
        recipient_code = response.json()['data']['recipient_code']
        
        # Initiate transfer
        transfer_url = "https://api.paystack.co/transfer"
        transfer_data = {
            "source": "balance",
            "amount": amount * 100,  # Paystack uses kobo
            "recipient": recipient_code,
            "reason": f"Transfer via WhatsApp Bot by {user_number}"
        }
        logging.info(f"Initiating transfer of {amount} NGN to {account_number}")
        response = requests.post(transfer_url, headers=headers, json=transfer_data)
        if response.status_code != 200:
            logging.error(f"Transfer failed: {response.text}")
            return False, f"Transfer failed: {response.status_code}"
        
        transfer_code = response.json()['data']['transfer_code']
        fee = response.json()['data'].get('fee', 0) // 100  # Convert from kobo
        
        # Deduct from user balance
        url = urlparse(os.getenv("DATABASE_URL"))
        conn = psycopg2.connect(
            database=url.path[1:],
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port
        )
        c = conn.cursor()
        c.execute("UPDATE users SET balance = balance - %s WHERE phone_number = %s", (amount, user_number))
        c.execute(
            "INSERT INTO transactions (user_number, account_number, bank_name, account_name, amount, fee, type, status, transfer_code) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (user_number, account_number, bank_name, account_name, amount, fee, "transfer", "Pending", transfer_code)
        )
        conn.commit()
        conn.close()
        logging.info(f"Transfer logged for {account_number}, amount: {amount}, fee: {fee}, transfer_code: {transfer_code}")
        return True, transfer_code
    except Exception as e:
        logging.error(f"Transfer error: {str(e)}")
        return False, f"Transfer error: {str(e)}"

# Fund user wallet
def fund_wallet(user_number, amount):
    try:
        # Initialize Paystack payment
        payment_url = "https://api.paystack.co/transaction/initialize"
        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
        reference = str(uuid.uuid4())
        payment_data = {
            "email": f"{user_number}@moneytransfermvp.com",  # Dummy email
            "amount": amount * 100,  # Paystack uses kobo
            "reference": reference,
            "callback_url": "https://money-transfer-mvp.onrender.com/payment/callback"
        }
        logging.info(f"Initializing payment of {amount} NGN for {user_number}")
        response = requests.post(payment_url, headers=headers, json=payment_data)
        if response.status_code != 200:
            logging.error(f"Failed to initialize payment: {response.text}")
            return None, f"Failed to initialize payment: {response.status_code}"
        
        payment_link = response.json()['data']['authorization_url']
        return payment_link, reference
    except Exception as e:
        logging.error(f"Payment initialization error: {str(e)}")
        return None, f"Payment error: {str(e)}"

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

# Payment callback
@app.route("/payment/callback", methods=["GET"])
def payment_callback():
    reference = request.args.get("reference")
    if not reference:
        logging.error("No reference provided in payment callback")
        return jsonify({"status": "error", "message": "No reference provided"})
    
    try:
        send_whatsapp_message(user_number, "Processing payment verification...")
        # Verify payment
        verify_url = f"https://api.paystack.co/transaction/verify/{reference}"
        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
        response = requests.get(verify_url, headers=headers)
        if response.status_code != 200:
            logging.error(f"Failed to verify payment: {response.text}")
            return jsonify({"status": "error", "message": "Payment verification failed"})
        
        data = response.json()
        if data['status'] and data['data']['status'] == "success":
            amount = data['data']['amount'] // 100  # Convert from kobo
            fee = data['data'].get('fees', 0) // 100  # Convert from kobo
            user_number = data['data']['customer']['email'].split('@')[0]
            
            # Update user balance
            url = urlparse(os.getenv("DATABASE_URL"))
            conn = psycopg2.connect(
                database=url.path[1:],
                user=url.username,
                password=url.password,
                host=url.hostname,
                port=url.port
            )
            c = conn.cursor()
            c.execute("UPDATE users SET balance = balance + %s WHERE phone_number = %s", (amount, user_number))
            c.execute(
                "INSERT INTO transactions (user_number, amount, fee, type, status, reference) VALUES (%s, %s, %s, %s, %s, %s)",
                (user_number, amount, fee, "fund", "Success", reference)
            )
            conn.commit()
            conn.close()
            logging.info(f"Added {amount} NGN to balance for {user_number}, fee: {fee}")
            send_whatsapp_message(user_number, f"Wallet funded with ₦{amount}! Your balance is now ₦{get_user_balance(user_number)}.")
            return jsonify({"status": "success"})
        else:
            logging.error(f"Payment verification failed: {data['message']}")
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
                "INSERT INTO transactions (user_number, amount, type, status, reference) VALUES (%s, %s, %s, %s, %s)",
                (user_number, 0, "fund", "Failed", reference)
            )
            conn.commit()
            conn.close()
            send_whatsapp_message(user_number, f"Wallet funding failed. Reference: {reference}. Please try again.")
            return jsonify({"status": "error", "message": "Payment verification failed"})
    except Exception as e:
        logging.error(f"Payment callback error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)})

# Transfer status webhook
@app.route("/transfer/webhook", methods=["POST"])
def transfer_webhook():
    data = request.json
    event = data.get("event")
    transfer_data = data.get("data")
    logging.info(f"Received webhook event: {event}, data: {transfer_data}")
    
    if event == "transfer.success":
        transfer_code = transfer_data.get("transfer_code")
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
                "UPDATE transactions SET status = %s WHERE transfer_code = %s",
                ("Success", transfer_code)
            )
            conn.commit()
            conn.close()
            logging.info(f"Updated transaction status to Success for transfer_code: {transfer_code}")
            user_number = transfer_data.get("recipient", {}).get("details", {}).get("phone") or \
                          transfer_data.get("reason", "").split("by ")[-1]
            if user_number:
                send_whatsapp_message(user_number, f"Transfer of ₦{transfer_data['amount']/100} completed! Transaction ID: {transfer_code}")
        except Exception as e:
            logging.error(f"Failed to update transaction status: {str(e)}")
    elif event == "transfer.failed":
        transfer_code = transfer_data.get("transfer_code")
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
                "UPDATE transactions SET status = %s WHERE transfer_code = %s",
                ("Failed", transfer_code)
            )
            # Refund user balance
            c.execute(
                "SELECT user_number, amount FROM transactions WHERE transfer_code = %s",
                (transfer_code,)
            )
            user_number, amount = c.fetchone()
            c.execute(
                "UPDATE users SET balance = balance + %s WHERE phone_number = %s",
                (amount, user_number)
            )
            conn.commit()
            conn.close()
            logging.info(f"Updated transaction status to Failed and refunded ₦{amount} for transfer_code: {transfer_code}")
            if user_number:
                send_whatsapp_message(user_number, f"Transfer of ₦{transfer_data['amount']/100} failed. Transaction ID: {transfer_code}. ₦{amount} refunded to your wallet.")
        except Exception as e:
            logging.error(f"Failed to update transaction status: {str(e)}")
    
    return jsonify({"status": "success"})

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

    if message_body.startswith("/fund"):
        send_whatsapp_message(from_number, "Processing wallet funding...")
        try:
            amount = int(message_body.split()[1])
            if amount <= 0:
                send_whatsapp_message(from_number, "Please specify a valid amount (e.g., /fund 10000).")
                return jsonify({"status": "error"})
            payment_link, reference = fund_wallet(from_number, amount)
            if payment_link:
                send_whatsapp_message(from_number, f"Please fund your wallet with ₦{amount} here: {payment_link}")
            else:
                send_whatsapp_message(from_number, reference)  # Error message
            return jsonify({"status": "success"})
        except (IndexError, ValueError):
            send_whatsapp_message(from_number, "Please specify a valid amount (e.g., /fund 10000).")
            return jsonify({"status": "error"})
    elif message_body == "/balance":
        send_whatsapp_message(from_number, "Processing balance check...")
        balance = get_user_balance(from_number)
        if balance is not None:
            send_whatsapp_message(from_number, f"Your wallet balance is ₦{balance}.")
        else:
            send_whatsapp_message(from_number, "Error fetching balance. Please try again.")
        return jsonify({"status": "success"})
    elif message_body == "/history":
        send_whatsapp_message(from_number, "Processing transaction history...")
        history = get_transaction_history(from_number)
        if history:
            send_whatsapp_message(from_number, "Recent transactions:\n" + "\n".join(history))
        else:
            send_whatsapp_message(from_number, "No transactions found or error fetching history.")
        return jsonify({"status": "success"})
    elif message_body == "/reconcile":
        send_whatsapp_message(from_number, "Processing balance reconciliation...")
        success, message = reconcile_balances()
        send_whatsapp_message(from_number, message)
        return jsonify({"status": "success"})
    elif media_url:
        send_whatsapp_message(from_number, "Processing image...")
        logging.info(f"Processing media from URL: {media_url}")
        image_path = os.path.join(os.getcwd(), "temp_image.jpg")
        logging.info(f"Saving image to: {image_path}")
        try:
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
                "account_name": account_name,
                "state": "awaiting_amount"
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
    elif message_body.isdigit() and from_number in app.sessions and app.sessions[from_number]["state"] == "awaiting_amount":
        send_whatsapp_message(from_number, "Processing transfer amount...")
        amount = int(message_body)
        session = app.sessions[from_number]
        session["amount"] = amount
        session["state"] = "awaiting_confirmation"
        confirmation_message = (
            f"Confirm transfer:\n"
            f"To: {session['account_name']}\n"
            f"Bank: {session['details']['bank_name']}\n"
            f"Account: {session['details']['account_number']}\n"
            f"Amount: ₦{amount}\n"
            f"Reply 'confirm' to proceed or 'cancel' to abort."
        )
        send_whatsapp_message(from_number, confirmation_message)
    elif message_body in ["confirm", "cancel"] and from_number in app.sessions and app.sessions[from_number]["state"] == "awaiting_confirmation":
        send_whatsapp_message(from_number, "Processing transfer confirmation...")
        session = app.sessions[from_number]
        if message_body == "cancel":
            send_whatsapp_message(from_number, "Transfer cancelled.")
            del app.sessions[from_number]
        else:
            success, result = initiate_transfer(
                session["details"]["account_number"],
                session["details"]["bank_name"],
                session["amount"],
                session["account_name"],
                from_number
            )
            if success:
                send_whatsapp_message(from_number, f"Transfer of ₦{session['amount']} to {session['account_name']} initiated! Transaction ID: {result}. You’ll get a confirmation soon.")
            else:
                send_whatsapp_message(from_number, result)
            del app.sessions[from_number]
    else:
        send_whatsapp_message(from_number, "Please send a clear image of typed or printed account details (bank name and 10-digit account number), or use /fund <amount>, /balance, /history, or /reconcile.")
    
    return jsonify({"status": "success"})

if __name__ == "__main__":
    init_db()
    fetch_banks()  # Preload banks
    app.run(debug=True)