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
import psycopg2


app = Flask(__name__)
cors = CORS(app)
load_dotenv()
#app.config['CORS_HEADERS'] = 'Content-Type'


client_id = os.getenv('CLIENT_ID')
client_secret = os.getenv('CLIENT_SECRET')
DATABASE_URL = os.getenv("DATABASE_URL")
#environment = SandboxEnvironment(client_id=client_id, client_secret=client_secret)
environment = LiveEnvironment(client_id=client_id, client_secret=client_secret)
client = PayPalHttpClient(environment)


def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1) license
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS "license" (
            id SERIAL PRIMARY KEY,
            licenseKey TEXT UNIQUE NOT NULL,
            generatedAt TIMESTAMP NOT NULL,
            expirationDate TIMESTAMP,
            used BOOLEAN DEFAULT FALSE,
            user_hash TEXT
        )
    ''')

    # 2) users (no affiliate_id column)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS "users" (
            id SERIAL PRIMARY KEY,
            name TEXT,
            email TEXT UNIQUE,
            password TEXT,
            phone TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            license_id INTEGER,
            createdAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (license_id) REFERENCES "license"(id)
        )
    ''')

    # 3) affiliate references users
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS "affiliate" (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            referralCode TEXT UNIQUE,
            totalReferrals INTEGER DEFAULT 0,
            successfulReferrals INTEGER DEFAULT 0,
            earnings REAL DEFAULT 0.0,
            createdAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES "users"(id)
        )
    ''')

    # 5) payment
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS "payment" (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            status TEXT,
            paymentMethod TEXT,
            paidAt TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES "users"(id)
        )
    ''')

    # 4) referral
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS "referral" (
            id SERIAL PRIMARY KEY,
            affiliate_id INTEGER,
            referred_user_id INTEGER,
            payment_id INTEGER,
            isSuccessful BOOLEAN,
            createdAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (affiliate_id) REFERENCES "affiliate"(id),
            FOREIGN KEY (referred_user_id) REFERENCES "users"(id),
            FOREIGN KEY (payment_id) REFERENCES "payment"(id)
        )
    ''')

    # 6) affiliatePayout
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS "affiliatePayout" (
            id SERIAL PRIMARY KEY,
            affiliate_id INTEGER,
            amount REAL,
            payoutMethod TEXT,
            status TEXT,
            paidAt TIMESTAMP,
            FOREIGN KEY (affiliate_id) REFERENCES "affiliate"(id)
        )
    ''')

    # 7) warning
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS "warning" (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            reason TEXT,
            resolved BOOLEAN,
            createdAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES "users"(id)
        )
    ''')

    # 8) trials
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS "trials" (
            id SERIAL PRIMARY KEY,
            user_hash TEXT UNIQUE,
            count INTEGER DEFAULT 0,
            updateDate TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Insert dummy licenses
    cursor.execute('''
        INSERT INTO "license" (licenseKey, generatedAt, expirationDate, used, user_hash)
        VALUES
        ('VALID_KEY_12345', '2024-08-25', '2025-08-25', FALSE, NULL),
        ('VALID_KEY_67890', '2024-08-26', '2025-08-26', FALSE, NULL)
        ON CONFLICT (licenseKey) DO NOTHING;
    ''')

    conn.commit()
    cursor.close()
    conn.close()


@app.route('/check_user', methods=['POST'])
def check_user():
    user_id = request.json.get('user_id')  # Get user_id from JSON request

    conn = get_db_connection()  # Use PostgreSQL connection
    cursor = conn.cursor()

    # Check if the user_hash exists in the License table
    cursor.execute('SELECT id FROM License WHERE user_hash = %s', (user_id,))
    license_record = cursor.fetchone()

    cursor.close()
    conn.close()

    return jsonify({'licensed': bool(license_record)})  # Return True if record exists, else False


@app.route('/validate', methods=['POST'])
def validate_license():
    data = request.json
    license_key = data.get('license_key')
    user_hash = data.get('user_id')

    conn = get_db_connection()  # Connect to PostgreSQL
    cursor = conn.cursor()

    # Check if the license exists and is not used
    cursor.execute('SELECT id, used FROM License WHERE licenseKey = %s', (license_key,))
    license_record = cursor.fetchone()

    if not license_record:
        cursor.close()
        conn.close()
        return jsonify({"valid": False, "licensed": False})  # License does not exist

    if license_record[1]:  # PostgreSQL uses TRUE/FALSE for booleans
        cursor.close()
        conn.close()
        return jsonify({"valid": False, "licensed": False})  # License is used

    # Mark the license as used and update user_hash
    cursor.execute('UPDATE License SET used = TRUE, user_hash = %s WHERE id = %s', (user_hash, license_record[0]))
    conn.commit()

    cursor.close()
    conn.close()

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

    conn = get_db_connection()  # Connect to PostgreSQL
    cursor = conn.cursor()

    try:
        # Generate a new license key
        license_key = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
        generated_at = '2024-08-26'
        expiration_date = '2025-08-26'  # Consider making this dynamic

        # Insert a new license
        cursor.execute('''
            INSERT INTO License (licenseKey, generatedAt, expirationDate, used, user_hash)
            VALUES (%s, %s, %s, FALSE, NULL)
            RETURNING id
        ''', (license_key, generated_at, expiration_date))
        license_id = cursor.fetchone()[0]  # Retrieve the newly inserted license ID

        # Insert user data with the associated license ID
        cursor.execute('''
            INSERT INTO Users (name, email, phone, address, city, state, zip, license_id, createdAt, updatedAt)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ''', (name, email, phone, address, city, state, zip_code, license_id))

        conn.commit()
        return jsonify({"message": "User data received and stored successfully", "license_key": license_key}), 201

    except Exception as e:
        conn.rollback()  # Rollback in case of an error
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()



@app.route('/generate_license', methods=['GET'])
def generate_license():
    """Endpoint to generate a random license key"""
    license_key = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
    generated_at = '2024-08-26'
    expiration_date = '2025-08-26'  # Consider making this dynamic

    conn = get_db_connection()  # Connect to PostgreSQL
    cursor = conn.cursor()

    try:
        # Insert the generated license into the License table
        cursor.execute('''
            INSERT INTO License (licenseKey, generatedAt, expirationDate, used, user_hash)
            VALUES (%s, %s, %s, FALSE, NULL)
        ''', (license_key, generated_at, expiration_date))

        conn.commit()
        print(f"Generated License Key: {license_key}")
        return jsonify({"license_key": license_key}), 201

    except Exception as e:
        conn.rollback()  # Rollback transaction if an error occurs
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()


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

    conn = get_db_connection()  # Connect to PostgreSQL
    cursor = conn.cursor()

    try:
        # Check if user_hash exists
        cursor.execute('SELECT id FROM Trials WHERE user_hash = %s', (user_hash,))
        trial_record = cursor.fetchone()

        if trial_record:
            # Update existing record
            trial_id = trial_record[0]
            cursor.execute(
                'UPDATE Trials SET count = %s, updateDate = CURRENT_TIMESTAMP WHERE id = %s',
                (count, trial_id)
            )
        else:
            # Create new record
            cursor.execute(
                'INSERT INTO Trials (user_hash, count, updateDate) VALUES (%s, %s, CURRENT_TIMESTAMP)',
                (user_hash, count)
            )

        conn.commit()
        return jsonify({'user_hash': user_hash, 'count': count})

    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500

    finally:
        cursor.close()
        conn.close()


@app.route('/free_trial_count', methods=['POST'])
def free_trial_count():
    data = request.json
    user_hash = data.get('user_hash')

    if not user_hash:
        return jsonify({'error': 'user_hash is required'}), 400

    conn = get_db_connection()  # Connect to PostgreSQL
    cursor = conn.cursor()

    try:
        cursor.execute('SELECT count FROM Trials WHERE user_hash = %s', (user_hash,))
        trial_record = cursor.fetchone()

        count = trial_record[0] if trial_record else 0  # Default to 0 if not found

        return jsonify({'user_hash': user_hash, 'count': count})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

    finally:
        cursor.close()
        conn.close()


@app.route("/initdb", methods=["GET"])
def init_db_endpoint():
    try:
        init_db()
        return jsonify({"status": "success", "message": "Database tables created/updated."}), 200
    except Exception as e:
        # Log or return the full error
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0")
