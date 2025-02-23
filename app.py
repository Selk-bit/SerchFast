from flask import Flask, request, jsonify
import sqlite3
import os
import random
import string
import paypalrestsdk
from paypalcheckoutsdk.orders import OrdersCreateRequest, OrdersCaptureRequest, OrdersGetRequest
from paypalcheckoutsdk.core import PayPalHttpClient, SandboxEnvironment, LiveEnvironment
from flask_cors import CORS, cross_origin
from dotenv import load_dotenv


app = Flask(__name__)
cors = CORS(app)
load_dotenv()
#app.config['CORS_HEADERS'] = 'Content-Type'

DATABASE = os.path.join(os.getcwd(), 'licenses.db')

#paypalrestsdk.configure({
#    "mode": "sandbox",  # or "live"
#    "client_id": "AY1TX4NMm67FzQgWPrZi9XyTOSQk01aHdE-ynchxQI68P6HUdWYHuog35W1PwhudCI1CvS_7pBYW2xC0",
#    "client_secret": "EKO0Aym-inhhJ7IOc211VzqIzGLvfjMkgl8drmYa8n_lQVVU3oZp06Kr7-foPd1AB2LUC24XMZ0T9SK0"
#})

client_id = os.getenv('CLIENT_ID')
client_secret = os.getenv('CLIENT_SECRET')
#environment = SandboxEnvironment(client_id=client_id, client_secret=client_secret)
environment = LiveEnvironment(client_id=client_id, client_secret=client_secret)
client = PayPalHttpClient(environment)


def init_db():
    if not os.path.exists(DATABASE):
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()

        # Create User table with nullable fields
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS User (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                email TEXT,
                password TEXT,
                phone TEXT,
                address TEXT,
                city TEXT,
                state TEXT,
                zip TEXT,
                license_id INTEGER,
                affiliate_id INTEGER,
                createdAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                updatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (license_id) REFERENCES License(id),
                FOREIGN KEY (affiliate_id) REFERENCES Affiliate(id)
            )
        ''')

        # Create License table with "used" and "user_hash" fields
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS License (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                licenseKey TEXT UNIQUE NOT NULL,
                generatedAt DATETIME NOT NULL,
                expirationDate DATETIME,
                used BOOLEAN DEFAULT 0,  -- 0: Not used, 1: Used
                user_hash TEXT
            )
        ''')

        # Create Affiliate table with nullable fields
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Affiliate (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                referralCode TEXT,
                totalReferrals INTEGER DEFAULT 0,
                successfulReferrals INTEGER DEFAULT 0,
                earnings REAL DEFAULT 0.0,
                createdAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                updatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES User(id)
            )
        ''')

        # Create Referral table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Referral (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                affiliate_id INTEGER,
                referred_user_id INTEGER,
                payment_id INTEGER,
                isSuccessful BOOLEAN,
                createdAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (affiliate_id) REFERENCES Affiliate(id),
                FOREIGN KEY (referred_user_id) REFERENCES User(id),
                FOREIGN KEY (payment_id) REFERENCES Payment(id)
            )
        ''')

        # Create Payment table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Payment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                status TEXT,
                paymentMethod TEXT,
                paidAt DATETIME,
                FOREIGN KEY (user_id) REFERENCES User(id)
            )
        ''')

        # Create AffiliatePayout table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS AffiliatePayout (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                affiliate_id INTEGER,
                amount REAL,
                payoutMethod TEXT,
                status TEXT,
                paidAt DATETIME,
                FOREIGN KEY (affiliate_id) REFERENCES Affiliate(id)
            )
        ''')

        # Create Warning table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Warning (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                reason TEXT,
                resolved BOOLEAN,
                createdAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                updatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES User(id)
            )
        ''')

        # Insert dummy data into License table
        dummy_licenses = [
            ("VALID_KEY_12345", '2024-08-25', '2025-08-25', 0, None),
            ("VALID_KEY_67890", '2024-08-26', '2025-08-26', 0, None)
        ]
        cursor.executemany('''
            INSERT OR IGNORE INTO License (licenseKey, generatedAt, expirationDate, used, user_hash)
            VALUES (?, ?, ?, ?, ?)
        ''', dummy_licenses)

        conn.commit()
        conn.close()


@app.route('/check_user', methods=['POST'])
def check_user():
    user_id = request.json['user_id']  # user_id contains the user_hash
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    # Check if there is any record in the License table with the received user_hash
    cursor.execute('SELECT id FROM License WHERE user_hash = ?', (user_id,))
    license_record = cursor.fetchone()

    conn.close()

    if license_record:
        return jsonify({'licensed': True})
    else:
        return jsonify({'licensed': False})


@app.route('/validate', methods=['POST'])
def validate_license():
    data = request.json
    license_key = data.get('license_key')
    user_hash = data.get('user_id')

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()

        # Check if the license exists and is not used
        cursor.execute('SELECT id, used FROM License WHERE licenseKey = ?', (license_key,))
        license_record = cursor.fetchone()

        if not license_record:
            return jsonify({"valid": False, "licensed": False})  # License does not exist

        if license_record[1] == 1:
            return jsonify({"valid": False, "licensed": False})  # License is used

        # Mark the license as used and update user_hash
        cursor.execute('UPDATE License SET used = 1, user_hash = ? WHERE id = ?', (user_hash, license_record[0]))
        conn.commit()

        return jsonify({"valid": True, "licensed": True})


@app.route('/submit_user_data', methods=['POST'])
def submit_user_data():
    """Endpoint to receive user's data after payment"""
    data = request.json
    name = data.get('name')
    email = data.get('email')
    phone = data.get('phone')
    address = data.get('address')
    city = data.get('city')
    state = data.get('state')
    zip_code = data.get('zip')

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()

        # Generate a new license if none is available
        license_key = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
        generated_at = '2024-08-26'
        expiration_date = '2025-08-26'  # You may generate the date dynamically

        cursor.execute('''
            INSERT INTO License (licenseKey, generatedAt, expirationDate, used, user_hash)
            VALUES (?, ?, ?, 0, NULL)
        ''', (license_key, generated_at, expiration_date))
        license_id = cursor.lastrowid

        # Insert user data into the User table with associated license
        cursor.execute('''
            INSERT INTO User (name, email, phone, address, city, state, zip, license_id, createdAt, updatedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime("now"), datetime("now"))
        ''', (name, email, phone, address, city, state, zip_code, license_id))

        conn.commit()
        return jsonify({"message": "User data received and stored successfully", "license_key": license_key}), 201



@app.route('/generate_license', methods=['GET'])
def generate_license():
    """Endpoint to generate a random license key"""
    license_key = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
    generated_at = '2024-08-26'
    expiration_date = '2025-08-26'  # You may generate the date dynamically

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        # Store the generated license in the License table
        cursor.execute('''
            INSERT INTO License (licenseKey, generatedAt, expirationDate, used, user_hash)
            VALUES (?, ?, ?, 0, NULL)
        ''', (license_key, generated_at, expiration_date))
        conn.commit()

    print(f"Generated License Key: {license_key}")
    return jsonify({"license_key": license_key})



@app.route('/get-amount', methods=['GET'])
def get_amount():
    amount = 30.00  # Example fixed amount
    return jsonify({"amount": f"{amount:.2f}"}), 200



@app.route('/create-order', methods=['POST'])
def create_order():
    # Construct a request object and set desired parameters
    create_order_request = OrdersCreateRequest()
    create_order_request.prefer('return=representation')
    create_order_request.request_body({
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {
                "currency_code": "USD",
                "value": "29.99"
            }
        }]
    })

    try:
        # Call PayPal to create the order
        response = client.execute(create_order_request)
        print('Order ID:', response.result.id)
        return jsonify({"orderID": response.result.id}), 200
    except IOError as ioe:
        print("Error creating order:", ioe)
        return jsonify({"error": str(ioe)}), 500


@app.route('/capture-order', methods=['POST'])
def capture_order():
    data = request.json
    order_id = data.get("orderID")

    if not order_id:
        return jsonify({"error": "Order ID is required"}), 400

    # Check the order status before capturing
    get_request = OrdersGetRequest(order_id)
    try:
        # Fetch the order details
        response = client.execute(get_request)
        order_status = response.result.status

        if order_status == "COMPLETED":
            # Order has already been captured
            return jsonify({"status": "error", "message": "Order has already been captured."}), 400
        elif order_status == "APPROVED":
            # Order is ready to be captured
            capture_request = OrdersCaptureRequest(order_id)
            capture_response = client.execute(capture_request)
            print('Capture ID:', capture_response.result.id)
            return jsonify({"status": "success", "captureID": capture_response.result.id}), 200
        else:
            # Handle other possible order statuses
            return jsonify({"status": "error", "message": f"Order cannot be captured in its current status: {order_status}."}), 400
    except IOError as ioe:
        print("Error capturing order:", ioe)
        return jsonify({"error": str(ioe)}), 500


@app.route('/update_free_trial', methods=['POST'])
def update_free_trial():
    data = request.json
    user_hash = data.get('user_hash')
    count = data.get('count')
    if not user_hash:
        return jsonify({'error': 'user_hash is required'}), 400
    if count is None:
        return jsonify({'error': 'count is required'}), 400

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        # Check if user_hash exists
        cursor.execute('SELECT id FROM Trials WHERE user_hash = ?', (user_hash,))
        trial_record = cursor.fetchone()
        if trial_record:
            # Update existing record
            trial_id = trial_record[0]
            cursor.execute('UPDATE Trials SET count = ?, updateDate = datetime("now") WHERE id = ?', (count, trial_id))
        else:
            # Create new record with count = 1
            cursor.execute('INSERT INTO Trials (user_hash, count, updateDate) VALUES (?, ?, datetime("now"))',
                           (user_hash, count))
        conn.commit()
        return jsonify({'user_hash': user_hash, 'count': count})


@app.route('/free_trial_count', methods=['POST'])
def free_trial_count():
    data = request.json
    user_hash = data.get('user_hash')
    if not user_hash:
        return jsonify({'error': 'user_hash is required'}), 400

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT count FROM Trials WHERE user_hash = ?', (user_hash,))
        trial_record = cursor.fetchone()
        if trial_record:
            count, = trial_record
            return jsonify({'user_hash': user_hash, 'count': count})
        else:
            # If user_hash is not found, return count as 0
            return jsonify({'user_hash': user_hash, 'count': 0})


if __name__ == '__main__':
    #init_db()
    app.run(debug=True, host="0.0.0.0")
