from flask import Flask, render_template, request ,session,redirect,jsonify,make_response
from pymongo import MongoClient
from datetime import datetime, timedelta
from bson.objectid import ObjectId
from dotenv import load_dotenv
import os
import re
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask_cors import CORS

load_dotenv()



usr_name = os.getenv("MONGO_USERNAME")
usr_password = os.getenv("MONGO_PASSWORD")
mongo_uri = os.getenv("MONGO_URI")

client = MongoClient(mongo_uri)

app = Flask(__name__)
app.secret_key=os.getenv("FLASK_SECRET_KEY")
turnstile_secret_key = os.getenv("TURNSTILE_SECRET_KEY")

cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "https://globsoft.vercel.app,http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
CORS(app, resources={r"/api/*": {
    "origins": cors_origins,
    "methods": ["GET", "POST", "OPTIONS"],
    "max_age": 300,
}})

# Cache for both status and products
_status_cache = {"data": None, "timestamp": None}
_products_cache = {"data": None, "timestamp": None}
_subscribe_attempts = {}

def no_cache(f):
    def decorated_function(*args, **kwargs):
        response = make_response(f(*args, **kwargs))
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    decorated_function.__name__ = f.__name__
    return decorated_function


def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def get_client_ip():
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )


def verify_turnstile(token):
    if not turnstile_secret_key or not token:
        return False

    try:
        payload = urlencode({
            "secret": turnstile_secret_key,
            "response": token,
            "remoteip": get_client_ip(),
        }).encode("utf-8")
        verification_request = Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(verification_request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result.get("success", False)
    except Exception:
        app.logger.exception("Turnstile verification error")
        return False


def is_rate_limited(ip_address):
    now = time.monotonic()
    window_seconds = 600
    max_attempts = 5
    attempts = [attempt for attempt in _subscribe_attempts.get(ip_address, []) if now - attempt < window_seconds]
    if len(attempts) >= max_attempts:
        _subscribe_attempts[ip_address] = attempts
        return True
    attempts.append(now)
    _subscribe_attempts[ip_address] = attempts
    return False


@app.route("/api/subscribe", methods=['POST'])
def subscribe():
    """Add a Globsoft Tech newsletter subscriber."""
    try:
        client_ip = get_client_ip()
        if is_rate_limited(client_ip):
            return jsonify({
                "success": False,
                "error": "Too many attempts. Please try again later.",
            }), 429

        data = request.get_json(silent=True) or {}
        email = str(data.get("email", "")).strip().lower()
        turnstile_token = str(data.get("turnstile_token", "")).strip()

        if not verify_turnstile(turnstile_token):
            return jsonify({
                "success": False,
                "error": "Security verification failed. Please try again.",
            }), 400

        if not email:
            return jsonify({"success": False, "error": "Email is required"}), 400

        if len(email) > 254 or not is_valid_email(email):
            return jsonify({"success": False, "error": "Invalid email format"}), 400

        collection = client["emails_db"]["emails"]
        if collection.find_one({"email": email, "source": "Globsoft Tech"}):
            return jsonify({"success": False, "error": "Email already subscribed"}), 409

        collection.insert_one({
            "email": email,
            "source": "Globsoft Tech",
            "subscribed_at": datetime.utcnow(),
            "ip_address": client_ip,
        })

        return jsonify({
            "success": True,
            "message": "Successfully subscribed to newsletter",
        }), 201
    except Exception:
        app.logger.exception("Newsletter subscription error")
        return jsonify({"success": False, "error": "Unable to subscribe right now"}), 500

# ==================== SYSTEM STATUS ====================

@app.route("/api/system-status", methods=['GET'])
def get_system_status():
    global _status_cache
    
    if (_status_cache["data"] and _status_cache["timestamp"] and 
        datetime.now() - _status_cache["timestamp"] < timedelta(minutes=5)):
        response = jsonify(_status_cache["data"])
        response.headers['Cache-Control'] = 'public, max-age=300'
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    
    db = client["globsoft_db"]
    collection = db["system_status"]
    status = collection.find_one(sort=[("updated_at", -1)])
    
    if not status:
        data = {"status": "running", "message": "All Systems Operational"}
    else:
        data = {
            "status": status.get("status", "running"),
            "message": status.get("message", "All Systems Operational"),
            "updated_at": status.get("updated_at").isoformat() if status.get("updated_at") else None
        }
    
    _status_cache["data"] = data
    _status_cache["timestamp"] = datetime.now()
    
    response = jsonify(data)
    response.headers['Cache-Control'] = 'public, max-age=300'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


@app.route("/system-status", methods=['GET', 'POST'])
@no_cache
def manage_system_status():
    global _status_cache
    
    if 'user' in session and session['user'] == usr_name:
        db = client["globsoft_db"]
        collection = db["system_status"]
        
        if request.method == 'POST':
            status = request.form.get("status")
            message = request.form.get("message")
            
            status_data = {
                "status": status,
                "message": message,
                "updated_by": session['user'],
                "updated_at": datetime.now()
            }
            
            collection.insert_one(status_data)
            _status_cache = {"data": None, "timestamp": None}
            
            from flask import flash
            flash('System status updated successfully!', 'success')
            return redirect('/system-status')
        
        current_status = collection.find_one(sort=[("updated_at", -1)])
        history = list(collection.find().sort("updated_at", -1).limit(20))
        
        return render_template('system_status.html', 
                             current_status=current_status,
                             history=history)
    else:
        return redirect("/")


# ==================== PRODUCTS ====================

@app.route("/api/products", methods=['GET'])
def get_products():
    global _products_cache
    
    if (_products_cache["data"] and _products_cache["timestamp"] and 
        datetime.now() - _products_cache["timestamp"] < timedelta(minutes=5)):
        response = jsonify(_products_cache["data"])
        response.headers['Cache-Control'] = 'public, max-age=300'
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    
    db = client["globsoft_db"]
    collection = db["products"]
    products = list(collection.find().sort("order", 1))
    
    # Convert ObjectId to string
    for product in products:
        product['_id'] = str(product['_id'])
    
    _products_cache["data"] = products
    _products_cache["timestamp"] = datetime.now()
    
    response = jsonify(products)
    response.headers['Cache-Control'] = 'public, max-age=300'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


@app.route("/products-manager", methods=['GET', 'POST'])
@no_cache
def manage_products():
    global _products_cache
    
    if 'user' in session and session['user'] == usr_name:
        db = client["globsoft_db"]
        collection = db["products"]
        
        if request.method == 'POST':
            action = request.form.get("action")
            
            if action == "add":
                product_data = {
                    "id": int(request.form.get("product_id")),
                    "name": request.form.get("name"),
                    "image": request.form.get("image"),
                    "link": request.form.get("link"),
                    "desc": request.form.get("desc"),
                    "order": int(request.form.get("order", 999)),
                    "updated_at": datetime.now()
                }
                collection.insert_one(product_data)
                _products_cache = {"data": None, "timestamp": None}
                
                from flask import flash
                flash('Product added successfully!', 'success')
            
            elif action == "edit":
                product_id = request.form.get("edit_id")
                collection.update_one(
                    {"_id": ObjectId(product_id)},
                    {"$set": {
                        "name": request.form.get("name"),
                        "image": request.form.get("image"),
                        "link": request.form.get("link"),
                        "desc": request.form.get("desc"),
                        "order": int(request.form.get("order", 999)),
                        "updated_at": datetime.now()
                    }}
                )
                _products_cache = {"data": None, "timestamp": None}
                
                from flask import flash
                flash('Product updated successfully!', 'success')
            
            elif action == "delete":
                product_id = request.form.get("delete_id")
                collection.delete_one({"_id": ObjectId(product_id)})
                _products_cache = {"data": None, "timestamp": None}
                
                from flask import flash
                flash('Product deleted successfully!', 'success')
            
            return redirect('/products-manager')
        
        products = list(collection.find().sort("order", 1))
        return render_template('products_manager.html', products=products)
    else:
        return redirect("/")

@app.route("/",methods=['GET','POST'])
@no_cache
def dashboard():
    if ('user' in session and session['user']== usr_name):
        return render_template('dashboard.html')
    if request.method=="POST":
        username = request.form.get("uname")
        password = request.form.get("password")
        if(username==usr_name and password==usr_password):
            session['user']=username
            return render_template('dashboard.html')
    return render_template('login.html')

@app.route("/logout", methods=['POST'])
@no_cache
def logout():
    session.pop('user', None)
    return redirect("/")

@app.route("/customer", methods=['GET', 'POST'])
@no_cache
def customer():
    if 'user' in session and session['user'] == usr_name:
        db = client["customer_db"]
        collection = db["customers"]
        
        # Fetch all customers
        customers = list(collection.find())
        
        return render_template('customer.html', customers=customers)
    else:
        return redirect("/")


@app.route("/customer/add", methods=['POST'])
@no_cache
def add_customer():
    if 'user' in session and session['user'] == usr_name:
        db = client["customer_db"]
        collection = db["customers"]
        
        customer_name = request.form.get("customer_name")
        customer_location = request.form.get("customer_location")
        customer_phone = request.form.get("customer_phone")
        customer_remark = request.form.get("customer_remark", "")
        
        # Check if customer with same phone already exists
        existing_customer = collection.find_one({"phone": customer_phone})
        
        if existing_customer:
            # Customer already exists - return with error message
            from flask import flash
            flash(f'Customer with phone number {customer_phone} already exists!', 'error')
            return redirect('/customer')
        
        customer_data = {
            "name": customer_name,
            "location": customer_location,
            "phone": customer_phone,
            "remark": customer_remark,
            "status_call": False,
            "status_sms": False,
            "status_whatsapp": False,
            "status_email": False,
            "user": session['user'],
            "date_added": datetime.now()
        }
        
        collection.insert_one(customer_data)
        from flask import flash
        flash('Customer added successfully!', 'success')
        
        return redirect('/customer')
    else:
        return redirect("/")


@app.route("/customer/edit/<customer_id>", methods=['GET', 'POST'])
@no_cache
def edit_customer(customer_id):
    if 'user' in session and session['user'] == usr_name:
        db = client["customer_db"]
        collection = db["customers"]
        
        if request.method == 'POST':
            customer_name = request.form.get("customer_name")
            customer_location = request.form.get("customer_location")
            customer_phone = request.form.get("customer_phone")
            customer_remark = request.form.get("customer_remark", "")
            
            updated_data = {
                "name": customer_name,
                "location": customer_location,
                "phone": customer_phone,
                "remark": customer_remark,
                "date_updated": datetime.now()
            }
            
            collection.update_one(
                {"_id": ObjectId(customer_id)},
                {"$set": updated_data}
            )
            
            return redirect('/customer')
        else:
            # GET request - show edit form
            customer = collection.find_one({"_id": ObjectId(customer_id)})
            return render_template('edit_customer.html', customer=customer)
    else:
        return redirect("/")


@app.route("/customer/delete", methods=['POST'])
@no_cache
def delete_customer():
    if 'user' in session and session['user'] == usr_name:
        db = client["customer_db"]
        collection = db["customers"]
        
        customer_id = request.form.get("customer_id")
        
        collection.delete_one({"_id": ObjectId(customer_id)})
        
        return redirect('/customer')
    else:
        return redirect("/")


@app.route("/customer/status", methods=['POST'])
@no_cache
def update_customer_status():
    if 'user' in session and session['user'] == usr_name:
        db = client["customer_db"]
        collection = db["customers"]
        
        customer_id = request.form.get("customer_id")
        status_type = request.form.get("status_type")
        is_checked = request.form.get("is_checked") == 'true'
        
        # Update the specific status field in customer document
        status_field = f"status_{status_type}"
        
        collection.update_one(
            {"_id": ObjectId(customer_id)},
            {"$set": {status_field: is_checked, "date_updated": datetime.now()}}
        )
        
        return redirect('/customer')
    else:
        return redirect("/")

@app.route("/sanourl", methods=['GET', 'POST', 'DELETE'])
@no_cache
def sanourl():
    if 'user' in session and session['user'] == usr_name:
        db = client["sanourl_db"]
        collection = db["urls"]
        
        # Handle DELETE request for multiple URLs
        if request.method == 'DELETE':
            data = request.get_json()
            url_ids = data.get('ids', [])
            if url_ids:
                from bson.objectid import ObjectId
                result = collection.delete_many({
                    '_id': {'$in': [ObjectId(id) for id in url_ids]}
                })
                return jsonify({'success': True, 'deleted': result.deleted_count})
            return jsonify({'success': False, 'message': 'No IDs provided'})
        
        # Fetch all URLs
        urls = list(collection.find())
        total_urls = len(urls)
        
        return render_template('sanourl.html', urls=urls, total_urls=total_urls)
    else:
        return redirect("/")

@app.route("/newsletter", methods=['GET', 'POST', 'DELETE'])
@no_cache
def newsletter():
    if 'user' in session and session['user'] == usr_name:
        db = client["emails_db"]
        collection = db["emails"]
        
        # Handle DELETE request for multiple emails
        if request.method == 'DELETE':
            data = request.get_json()
            email_ids = data.get('ids', [])
            if email_ids:
                try:
                    result = collection.delete_many({
                        '_id': {'$in': [ObjectId(id) for id in email_ids]}
                    })
                    return jsonify({'success': True, 'deleted': result.deleted_count})
                except Exception as e:
                    return jsonify({'success': False, 'message': str(e)}), 400
            return jsonify({'success': False, 'message': 'No IDs provided'}), 400
        
        # Fetch all emails
        emails = list(collection.find())
        total_emails = len(emails)
        
        return render_template('newsletter.html', emails=emails, total_emails=total_emails)
    else:

        return redirect("/")

# Add this to your existing app.py file on the other domain

# ==================== GURUJI INSTRUCTOR VERIFICATION ====================

@app.route("/guruji-instructors", methods=['GET', 'POST'])
@no_cache
def guruji_instructors():
    """Manage Guruji instructor verification requests."""
    if 'user' in session and session['user'] == usr_name:
        # Connect to Guruji database
        guruji_db = client["guruji"]  # Make sure this matches your Guruji MongoDB database name
        users_collection = guruji_db["users"]
        
        if request.method == 'POST':
            action = request.form.get("action")
            user_id = request.form.get("user_id")
            
            if action == "approve":
                users_collection.update_one(
                    {"_id": ObjectId(user_id)},
                    {"$set": {"verification_status": "approved"}}
                )
                from flask import flash
                flash('Instructor approved successfully!', 'success')
            
            elif action == "reject":
                users_collection.update_one(
                    {"_id": ObjectId(user_id)},
                    {"$set": {"verification_status": "rejected", "role": "student"}}
                )
                from flask import flash
                flash('Instructor rejected.', 'info')
            
            return redirect('/guruji-instructors')
        
        # Get all pending instructor requests
        pending_instructors = list(users_collection.find({
            "role": "instructor",
            "verification_status": "pending"
        }).sort("created_at", -1))
        
        # Get all approved instructors
        approved_instructors = list(users_collection.find({
            "role": "instructor",
            "verification_status": "approved"
        }).sort("created_at", -1))
        
        # Get all rejected
        rejected_instructors = list(users_collection.find({
            "verification_status": "rejected"
        }).sort("created_at", -1).limit(20))
        
        return render_template('guruji_instructors.html', 
                             pending=pending_instructors,
                             approved=approved_instructors,
                             rejected=rejected_instructors)
    else:
        return redirect("/")


# Also add this API endpoint for cross-domain verification (optional)
@app.route("/api/guruji/verify-instructor", methods=['POST'])
def verify_instructor_api():
    """API endpoint to verify instructor (with API key auth)."""
    api_key = request.headers.get('X-API-Key')
    
    # Simple API key auth - set this in your .env
    if api_key != os.getenv("GURUJI_API_KEY"):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json()
    user_id = data.get('user_id')
    action = data.get('action')  # 'approve' or 'reject'
    
    if not user_id or action not in ['approve', 'reject']:
        return jsonify({'error': 'Invalid request'}), 400
    
    guruji_db = client["guruji"]
    users_collection = guruji_db["users"]
    
    if action == "approve":
        users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"verification_status": "approved"}}
        )
    else:
        users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"verification_status": "rejected", "role": "student"}}
        )
    
    return jsonify({'success': True, 'action': action})
