import html
from flask import Flask, flash, render_template, request, session, redirect, jsonify, make_response, Response, has_request_context
import hmac
import hashlib
from pymongo import MongoClient, ReturnDocument
from datetime import datetime, timedelta
from bson.objectid import ObjectId
from dotenv import load_dotenv
import os
import re
import json
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import base64
try:
    import requests
except ImportError:
    requests = None

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
load_dotenv()



usr_name = os.getenv("MONGO_USERNAME")
usr_password = os.getenv("MONGO_PASSWORD")
mongo_uri = os.getenv("MONGO_URI")
admin_email = os.getenv("ADMIN_EMAIL", "info.globsoft@gmail.com").strip().lower()
clerk_publishable_key = os.getenv("CLERK_PUBLISHABLE_KEY", "").strip()
clerk_secret_key = os.getenv("CLERK_SECRET_KEY", "").strip()

def get_clerk_frontend_api(publishable_key):
    if not publishable_key:
        return ""
    try:
        parts = publishable_key.split("_", 2)
        if len(parts) >= 3:
            raw = parts[2]
            padded = raw + "=" * (-len(raw) % 4)
            return base64.b64decode(padded).decode("utf-8").rstrip("$")
    except Exception:
        pass
    return ""

clerk_frontend_api = get_clerk_frontend_api(clerk_publishable_key)

client = MongoClient(mongo_uri) if mongo_uri else MongoClient("mongodb://localhost:27017")

app = Flask(__name__)
application = app
handler = app
app.secret_key = os.getenv("FLASK_SECRET_KEY") or "GlobSoftAdminWebAppSecureKey2026Production"
is_production = os.getenv("VERCEL") == "1" or os.getenv("FLASK_ENV") != "development"
app.config.update(
    SESSION_COOKIE_NAME="globsoft_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=is_production,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_PATH="/",
)
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
    "max_age": 0,
}})

# Cache for product responses only
_products_cache = {"data": None, "timestamp": None}
_subscribe_attempts = {}
BLOG_IMAGE_MAX_BYTES = 10 * 1024 * 1024
BLOG_VIDEO_MAX_BYTES = 15 * 1024 * 1024

def no_cache(f):
    def decorated_function(*args, **kwargs):
        response = make_response(f(*args, **kwargs))
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    decorated_function.__name__ = f.__name__
    return decorated_function


def is_admin():
    user = (session.get("user") or "").strip().lower()
    default_admin = (usr_name or "").strip().lower()
    return bool(user and (user == admin_email or user == default_admin or session.get("role") == "admin"))


def is_blog_author():
    return is_admin() or (session.get("role") == "author" and bool(session.get("author_id")))


def can_manage_blogs():
    return is_admin() or is_blog_author()


def slugify(value):
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return normalized or f"post-{int(time.time())}"


def calculate_read_time(content):
    """Estimate read time in minutes based on ~200 words per minute."""
    clean_text = re.sub(r"<[^>]+>", " ", content or "")
    word_count = len(clean_text.split())
    return max(1, round(word_count / 200))


def get_public_blog(document):
    content = document.get("content", "")
    author_avatar = document.get("author_avatar")
    author_bio = document.get("author_bio")
    author_email = (document.get("author_email") or document.get("created_by") or "").strip().lower()
    author_id = document.get("author_id")

    auth = None
    if author_email:
        auth = client["globsoft_db"]["authors"].find_one({"email": author_email})
    elif author_id:
        try:
            auth = client["globsoft_db"]["authors"].find_one({"_id": ObjectId(author_id)})
        except Exception:
            pass

    if auth:
        if not author_avatar and auth.get("avatar_url"):
            author_avatar = auth["avatar_url"]
        if not author_bio and auth.get("bio"):
            author_bio = auth["bio"]

    return {
        "id": str(document["_id"]),
        "title": document["title"],
        "slug": document["slug"],
        "excerpt": document["excerpt"],
        "content": content,
        "author": document.get("author") or (auth.get("name") if auth else "Globsoft Team"),
        "author_email": author_email or (auth.get("email") if auth else None),
        "author_avatar": author_avatar,
        "author_bio": author_bio,
        "category": document.get("category") or "Technology",
        "tags": document.get("tags") or "",
        "views": int(document.get("views", 0)),
        "read_time": document.get("read_time") or calculate_read_time(content),
        "comments": document.get("comments", []),
        "comments_count": len(document.get("comments", [])),
        "published_at": document.get("published_at").isoformat() if document.get("published_at") else None,
        "updated_at": document.get("updated_at").isoformat() if document.get("updated_at") else None,
        "image_url": f"/api/blog-images/{document['_id']}" if document.get("image_data") else (document.get("image_url") or None),
        "seo_title": document.get("seo_title") or document["title"],
        "seo_description": document.get("seo_description") or document["excerpt"],
    }


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


def get_gmail_smtp_credentials():
    sender = os.getenv("GMAIL_SENDER", "info.globsoft@gmail.com")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    missing = ["GMAIL_APP_PASSWORD"] if not app_password else []
    if missing:
        raise RuntimeError(
            "Gmail SMTP broadcast is not configured. Missing: " + ", ".join(missing)
        )
    return sender, app_password


def get_daily_recipient_limit():
    try:
        return max(1, int(os.getenv("GMAIL_DAILY_RECIPIENT_LIMIT", "400")))
    except ValueError:
        app.logger.warning("Invalid GMAIL_DAILY_RECIPIENT_LIMIT; using 400")
        return 400


def reserve_daily_recipients(sender, count):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    collection = client["emails_db"]["newsletter_delivery"]
    record = collection.find_one_and_update(
        {"_id": f"{sender}:{today}"},
        {
            "$inc": {"recipients": count},
            "$setOnInsert": {"sender": sender, "date": today},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    limit = get_daily_recipient_limit()
    if record["recipients"] > limit:
        collection.update_one(
            {"_id": record["_id"]},
            {"$inc": {"recipients": -count}},
        )
        return False, limit
    return True, limit

def send_gmail_message(
    smtp_connection,
    sender,
    recipients,
    subject,
    plain_text,
    html,
    preview=False,
):
    message = MIMEMultipart("alternative")
    message["From"] = sender
    message["To"] = sender if preview else "Undisclosed recipients:;"
    message["Subject"] = subject
    message.attach(MIMEText(plain_text, "plain", "utf-8"))
    if html:
        message.attach(MIMEText(html, "html", "utf-8"))

    smtp_connection.sendmail(sender, recipients, message.as_string())


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


DEFAULT_SITE_CONTENT = {
    "pricing": [
        {
            "id": "0",
            "title": "Starter Website",
            "description": "Perfect for small businesses getting online",
            "original_price": None,
            "price": "9999",
            "offer": "",
            "features": [
                "Responsive single-page design",
                "Basic animations & interactions",
                "Free business email (1 account)",
                "Social media integration (3 platforms)",
                "Hosting on domain (1 year)",
                "Basic SEO setup",
            ],
        },
        {
            "id": "1",
            "title": "Business Website",
            "description": "For growing businesses needing more impact",
            "original_price": None,
            "price": "24999",
            "offer": "",
            "features": [
                "Custom multi-page website",
                "Advanced animations & micro-interactions",
                "Business emails (5 accounts)",
                "Full social media integration (5 platforms)",
                "Custom domain (2 year included)",
                "Advanced SEO optimization",
                "Basic analytics dashboard",
            ],
        },
        {
            "id": "2",
            "title": "Enterprise Digital Suite",
            "description": "End-to-end digital transformation",
            "original_price": None,
            "price": None,
            "offer": "",
            "features": [
                "Custom mobile app (iOS/Android)",
                "Monthly social media management",
                "Complete branding package (logo, banners)",
                "Tech solutions",
                "Priority 24/7 support",
                "Advanced analytics portal",
            ],
        },
    ],
    "clients": [
        {"id": "0", "name": "Samira Homestay", "logo": "/src/assets/notification/image-5.png", "url": "https://samirahomestay.com/"},
        {"id": "1", "name": "News Arpan", "logo": "/src/assets/notification/image-4.png", "url": "https://newsarpan.rf.gd/"},
        {"id": "2", "name": "Everest Auto Repair", "logo": "/src/assets/notification/image-3.png", "url": "https://globsofttech.github.io/EverestAutoRepair/"},
        {"id": "3", "name": "Our Bakery", "logo": "/src/assets/notification/image-2.png", "url": "https://our-bakery.github.io/"},
    ],
    "testimonials": [
        {
            "id": "0",
            "name": "Samira Sharma",
            "role": "Owner & Host",
            "company": "Samira Homestay",
            "avatar": "/src/assets/notification/image-5.png",
            "quote": "Globsoft Tech built an exceptional website for our homestay. Inquiries from international and domestic guests increased dramatically right after launching!",
            "rating": 5,
            "url": "https://samirahomestay.com/"
        },
        {
            "id": "1",
            "name": "Arpan Sharma",
            "role": "Editor-in-Chief",
            "company": "News Arpan",
            "avatar": "/src/assets/notification/image-4.png",
            "quote": "Our news portal requires lightning-fast load times and high stability under heavy daily traffic. Globsoft Tech delivered a rock-solid, mobile-optimized experience.",
            "rating": 5,
            "url": "https://newsarpan.rf.gd/"
        },
        {
            "id": "2",
            "name": "Bikash Thapa",
            "role": "Managing Director",
            "company": "Everest Auto Repair",
            "avatar": "/src/assets/notification/image-3.png",
            "quote": "Partnering with Globsoft Tech revolutionized our digital presence. Our customers now easily discover our auto services and book repair appointments directly online.",
            "rating": 5,
            "url": "https://globsofttech.github.io/EverestAutoRepair/"
        },
        {
            "id": "3",
            "name": "Sunita Adhikari",
            "role": "Founder & Head Baker",
            "company": "Our Bakery",
            "avatar": "/src/assets/notification/image-2.png",
            "quote": "The team at Globsoft crafted a delightful, appetizing website for Our Bakery. It makes showcasing our bakery menu and catering offerings effortless.",
            "rating": 5,
            "url": "https://our-bakery.github.io/"
        }
    ],
    "team": [
        {
            "id": "1",
            "name": "Rajan Poudel",
            "role": "Founder & CEO",
            "bio": "Self-taught software developer, and Founder of Globsoft Tech.",
            "image": "/src/assets/rajan.jpg",
            "email": "rajanpoudel.np@gmail.com",
            "socials": {
                "instagram": "https://www.instagram.com/_rajanpoudel",
                "linkedin": "https://www.linkedin.com/in/rajanpoudel-np/",
                "github": "https://github.com/rajan-poudel",
            },
        },
        {
            "id": "2",
            "name": "Sourya Poudel",
            "role": "Chief Technology Officer",
            "bio": "Full-stack architect specializing in distributed cloud systems, cybersecurity, and high-performance web apps.",
            "image": "/src/assets/sourya.png",
            "email": "souryapoudel.np@gmail.com",
            "socials": {
                "instagram": "https://www.instagram.com/sourya_poudel_",
                "linkedin": "https://www.linkedin.com/in/sourya-poudel/",
                "github": "https://github.com/sourya-poudel",
            },
        },
        {
            "id": "3",
            "name": "Ankit Lamsal",
            "role": "Finance Head",
            "bio": "Oversees Globsoft Tech's finances, managing cash flow, budgeting, and financial planning to keep the company stable and growing.",
            "image": "/src/assets/ankit.jpg",
            "socials": {
                "instagram": "https://www.instagram.com/ankiit9638",
            },
        },
    ],
}


def get_site_content():
    content = None
    try:
        content = client["globsoft_db"]["site_content"].find_one({"_id": "homepage"})
    except Exception as e:
        app.logger.warning("Could not fetch site_content from database: %s", e)
    return {
        "pricing": content.get("pricing", DEFAULT_SITE_CONTENT["pricing"]) if content else DEFAULT_SITE_CONTENT["pricing"],
        "clients": content.get("clients", DEFAULT_SITE_CONTENT["clients"]) if content else DEFAULT_SITE_CONTENT["clients"],
        "testimonials": content.get("testimonials", DEFAULT_SITE_CONTENT["testimonials"]) if content else DEFAULT_SITE_CONTENT["testimonials"],
        "team": content.get("team", DEFAULT_SITE_CONTENT["team"]) if content else DEFAULT_SITE_CONTENT["team"],
    }


def get_team_members():
    content = get_site_content()
    base_team = list(content.get("team") or DEFAULT_SITE_CONTENT["team"])
    existing_identifiers = set()
    for m in base_team:
        if m.get("email"):
            existing_identifiers.add(m.get("email").strip().lower())
        if m.get("name"):
            existing_identifiers.add(m.get("name").strip().lower())

    try:
        authors_col = client["globsoft_db"]["authors"]
        blogs_col = client["globsoft_db"]["blogs"]
        active_authors = list(authors_col.find({"active": True}))

        for a in active_authors:
            # Check if this author has been hidden from the team page by admin
            if a.get("show_on_team") is False:
                continue

            a_id = str(a["_id"])
            a_email = (a.get("email") or "").strip().lower()

            # Count published blogs by this author (must have at least 1 published article)
            pub_count = blogs_col.count_documents({
                "status": "published",
                "$or": [
                    {"author_id": a_id},
                    {"author_email": a_email},
                    {"created_by": a_email},
                ]
            })

            # Strictly require at least 1 published article
            if pub_count < 1:
                continue

            # Skip if author is already in base team
            if a_email in existing_identifiers or (a.get("name") or "").strip().lower() in existing_identifiers:
                continue

            socials = a.get("socials") or {}
            base_team.append({
                "id": f"author-{a_id}",
                "name": a.get("name") or "Contributing Author",
                "role": a.get("role") or "Contributing Author",
                "bio": a.get("bio") or f"Published author at Globsoft Tech with {pub_count} {'articles' if pub_count > 1 else 'article'}.",
                "image": a.get("avatar_url") or "",
                "email": a.get("public_email") or a_email,
                "socials": socials,
                "is_author": True,
                "post_count": pub_count,
            })
    except Exception as e:
        app.logger.exception("Error gathering author team members: %s", e)

    return base_team


@app.route("/api/site-content", methods=["GET"])
def site_content():
    data = get_site_content()
    data["team_with_authors"] = get_team_members()
    response = jsonify(data)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/api/team", methods=["GET"])
def api_team():
    response = jsonify(get_team_members())
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


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


def send_admin_alert(subject, text_body, html_body=None):
    """Safely send an email notification to admin using configured Gmail credentials."""
    try:
        sender, app_password = get_gmail_smtp_credentials()
        dest = os.getenv("ADMIN_EMAIL", "info.globsoft@gmail.com")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Globsoft Tech Portal <{sender}>"
        msg["To"] = dest
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(sender, app_password)
            server.sendmail(sender, [dest], msg.as_string())
    except Exception as exc:
        app.logger.warning(f"Could not send admin alert email: {exc}")


@app.route("/api/contact", methods=["POST", "OPTIONS"])
def api_contact():
    """Receive contact form submissions and save directly to Customer Management."""
    if request.method == "OPTIONS":
        return "", 204

    try:
        client_ip = get_client_ip()
        if is_rate_limited(client_ip):
            return jsonify({
                "success": False,
                "error": "Too many requests. Please wait a moment."
            }), 429

        data = request.get_json(silent=True) or request.form.to_dict() or {}
        name = str(data.get("name", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        message = str(data.get("message", "")).strip()
        phone = str(data.get("phone", "")).strip()
        turnstile_token = str(data.get("turnstile_token", "")).strip()

        if turnstile_token and not verify_turnstile(turnstile_token):
            return jsonify({"success": False, "error": "Security verification failed."}), 400

        if not name:
            return jsonify({"success": False, "error": "Name is required."}), 400
        if not email or not is_valid_email(email):
            return jsonify({"success": False, "error": "A valid email address is required."}), 400
        if not message:
            return jsonify({"success": False, "error": "Message is required."}), 400

        customer_doc = {
            "name": name,
            "email": email,
            "phone": phone or "N/A",
            "location": "Website Contact",
            "source": "Contact Form",
            "type": "inquiry",
            "service": "General Inquiry",
            "company": "N/A",
            "remark": message,
            "status_call": False,
            "status_sms": False,
            "status_whatsapp": False,
            "status_email": False,
            "ip_address": client_ip,
            "user": "website_contact",
            "date_added": datetime.now()
        }

        db = client["customer_db"]
        res = db["customers"].insert_one(customer_doc)

        email_subj = f"📨 New Website Contact: {name}"
        email_text = f"New Contact Message received on Globsoft Tech:\n\nName: {name}\nEmail: {email}\nPhone: {phone or 'N/A'}\nMessage:\n{message}\n\nView in Admin: https://adminglobsoft.vercel.app/customer"
        send_admin_alert(email_subj, email_text)

        return jsonify({
            "success": True,
            "message": "Message sent successfully! We will get back to you shortly.",
            "id": str(res.inserted_id)
        }), 201

    except Exception as e:
        app.logger.exception("Contact submission error")
        return jsonify({"success": False, "error": "Server error processing your message. Please try again."}), 500


@app.route("/api/order", methods=["POST", "OPTIONS"])
def api_order():
    """Receive order form submissions and save directly to Customer Management."""
    if request.method == "OPTIONS":
        return "", 204

    try:
        client_ip = get_client_ip()
        if is_rate_limited(client_ip):
            return jsonify({
                "success": False,
                "error": "Too many requests. Please wait a moment."
            }), 429

        data = request.get_json(silent=True) or request.form.to_dict() or {}
        name = str(data.get("name", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        phone = str(data.get("phone", "")).strip()
        company = str(data.get("company", "")).strip()
        service = str(data.get("service", "")).strip()
        package = str(data.get("package", "")).strip()
        price = str(data.get("price", "")).strip()
        if not service and package:
            service = package
        message = str(data.get("message", "")).strip()
        turnstile_token = str(data.get("turnstile_token", "")).strip()

        if turnstile_token and not verify_turnstile(turnstile_token):
            return jsonify({"success": False, "error": "Security verification failed."}), 400

        if not name:
            return jsonify({"success": False, "error": "Full name is required."}), 400
        if not email or not is_valid_email(email):
            return jsonify({"success": False, "error": "A valid email address is required."}), 400
        if not phone:
            return jsonify({"success": False, "error": "Phone number is required."}), 400
        if not service and not package:
            return jsonify({"success": False, "error": "Service selection is required."}), 400
        if not message:
            return jsonify({"success": False, "error": "Project details are required."}), 400

        pkg_desc = f"Package: {package or service}" + (f" (रु {price})" if price else "")
        customer_doc = {
            "name": name,
            "email": email,
            "phone": phone,
            "company": company or "N/A",
            "location": company or "Website Order",
            "source": "Order Form",
            "type": "order",
            "service": service or package,
            "package": package or service,
            "price": price,
            "remark": f"{pkg_desc} | Company: {company or 'N/A'}\nProject Details: {message}",
            "project_details": message,
            "status_call": False,
            "status_sms": False,
            "status_whatsapp": False,
            "status_email": False,
            "ip_address": client_ip,
            "user": "website_order",
            "date_added": datetime.now()
        }

        db = client["customer_db"]
        res = db["customers"].insert_one(customer_doc)

        pkg_display = f"{package} (रु {price})" if (package and price) else (package or service)
        email_subj = f"🎉 New Order Received: {pkg_display} - {name}"
        email_text = f"New Project Order received on Globsoft Tech!\n\nClient Name: {name}\nEmail: {email}\nPhone: {phone}\nCompany: {company or 'N/A'}\nPackage / Service: {pkg_display}\n\nProject Details:\n{message}\n\nView in Admin: https://adminglobsoft.vercel.app/customer"
        send_admin_alert(email_subj, email_text)

        return jsonify({
            "success": True,
            "message": "Order submitted successfully! Our team will contact you within 24 hours.",
            "id": str(res.inserted_id)
        }), 201

    except Exception as e:
        app.logger.exception("Order submission error")
        return jsonify({"success": False, "error": "Server error processing your order. Please try again."}), 500

# ==================== SYSTEM STATUS ====================

@app.route("/api/system-status", methods=['GET'])
def get_system_status():
    """Return the latest status directly from MongoDB without response caching."""
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
    
    response = jsonify(data)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.route("/system-status", methods=['GET', 'POST'])
@no_cache
def manage_system_status():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")
    db = client["globsoft_db"]
    collection = db["system_status"]
    
    if request.method == 'POST':
        status = request.form.get("status", "").strip().lower()
        message = request.form.get("message")
        if status not in {"running", "maintenance", "offline"}:
            flash("Invalid system status.", "error")
            return redirect("/system-status")
        
        status_data = {
            "status": status,
            "message": message,
            "updated_by": session['user'],
            "updated_at": datetime.now()
        }
        
        collection.insert_one(status_data)
        
        flash('System status updated successfully!', 'success')
        return redirect('/system-status')
    
    current_status = collection.find_one(sort=[("updated_at", -1)])
    history = list(collection.find().sort("updated_at", -1).limit(20))
    
    return render_template('system_status.html', 
                         current_status=current_status,
                         history=history)


@app.route("/site-content", methods=["GET", "POST"])
@no_cache
def manage_site_content():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")

    collection = client["globsoft_db"]["site_content"]
    if request.method == "POST":
        try:
            pricing = json.loads(request.form.get("pricing_json", "[]"))
            clients = json.loads(request.form.get("clients_json", "[]"))
            if not isinstance(pricing, list) or not isinstance(clients, list):
                raise ValueError("Content must be arrays.")
            result = collection.replace_one(
                {"_id": "homepage"},
                {
                    "_id": "homepage",
                    "pricing": pricing,
                    "clients": clients,
                    "updated_at": datetime.now(),
                    "updated_by": session["user"],
                },
                upsert=True,
            )
            saved_content = collection.find_one({"_id": "homepage"})
            if not saved_content or saved_content.get("pricing") != pricing or saved_content.get("clients") != clients:
                raise RuntimeError("Homepage content was not persisted.")
            flash("Homepage content updated successfully.", "success")
        except (TypeError, ValueError, json.JSONDecodeError, RuntimeError) as error:
            app.logger.exception("Homepage content save failed: %s", error)
            flash("Could not save content. Check that both JSON fields are valid arrays.", "error")
        return redirect("/site-content")

    content = get_site_content()
    return render_template("site_content.html", content=content)


def save_site_content_field(field_name, value):
    collection = client["globsoft_db"]["site_content"]
    current = get_site_content()
    current[field_name] = value
    collection.replace_one(
        {"_id": "homepage"},
        {
            "_id": "homepage",
            "pricing": current.get("pricing", DEFAULT_SITE_CONTENT["pricing"]),
            "clients": current.get("clients", DEFAULT_SITE_CONTENT["clients"]),
            "testimonials": current.get("testimonials", DEFAULT_SITE_CONTENT["testimonials"]),
            "team": current.get("team", DEFAULT_SITE_CONTENT["team"]),
            "updated_at": datetime.now(),
            "updated_by": session.get("user", "admin"),
        },
        upsert=True,
    )
    saved_content = collection.find_one({"_id": "homepage"})
    if not saved_content or saved_content.get(field_name) != value:
        raise RuntimeError("Homepage content was not persisted.")


@app.route("/site-content/pricing", methods=["GET", "POST"])
@no_cache
def manage_pricing_content():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")

    if request.method == "POST":
        try:
            pricing = json.loads(request.form.get("pricing_json", "[]"))
            if not isinstance(pricing, list):
                raise ValueError("Pricing content must be an array.")
            save_site_content_field("pricing", pricing)
            flash("Pricing cards updated successfully.", "success")
        except (TypeError, ValueError, json.JSONDecodeError, RuntimeError) as error:
            app.logger.exception("Pricing content save failed: %s", error)
            flash("Could not save pricing. Check that the JSON is a valid array.", "error")
        return redirect("/site-content/pricing")

    content = get_site_content()
    return render_template(
        "pricing_content.html",
        pricing_json=json.dumps(content["pricing"], indent=2, ensure_ascii=False),
    )


@app.route("/site-content/clients", methods=["GET", "POST"])
@no_cache
def manage_client_content():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")

    if request.method == "POST":
        try:
            clients = json.loads(request.form.get("clients_json", "[]"))
            if not isinstance(clients, list):
                raise ValueError("Client content must be an array.")
            save_site_content_field("clients", clients)
            flash("Client showcase updated successfully.", "success")
        except (TypeError, ValueError, json.JSONDecodeError, RuntimeError) as error:
            app.logger.exception("Client content save failed: %s", error)
            flash("Could not save clients. Check that the JSON is a valid array.", "error")
        return redirect("/site-content/clients")

    content = get_site_content()
    return render_template(
        "clients_content.html",
        clients_json=json.dumps(content["clients"], indent=2, ensure_ascii=False),
    )


@app.route("/site-content/testimonials", methods=["GET", "POST"])
@no_cache
def manage_testimonials_content():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")

    if request.method == "POST":
        try:
            testimonials = json.loads(request.form.get("testimonials_json", "[]"))
            if not isinstance(testimonials, list):
                raise ValueError("Testimonials content must be an array.")
            save_site_content_field("testimonials", testimonials)
            flash("Testimonials updated successfully.", "success")
        except (TypeError, ValueError, json.JSONDecodeError, RuntimeError) as error:
            app.logger.exception("Testimonials content save failed: %s", error)
            flash("Could not save testimonials. Check that the JSON is a valid array.", "error")
        return redirect("/site-content/testimonials")

    content = get_site_content()
    return render_template(
        "testimonials_content.html",
        testimonials_json=json.dumps(content.get("testimonials", DEFAULT_SITE_CONTENT["testimonials"]), indent=2, ensure_ascii=False),
        testimonials=content.get("testimonials", DEFAULT_SITE_CONTENT["testimonials"]),
    )


@app.route("/site-content/team", methods=["GET", "POST"])
@no_cache
def manage_team_content():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")

    if request.method == "POST":
        try:
            team = json.loads(request.form.get("team_json", "[]"))
            if not isinstance(team, list):
                raise ValueError("Team content must be an array.")
            save_site_content_field("team", team)
            flash("Core team members updated successfully.", "success")
        except (TypeError, ValueError, json.JSONDecodeError, RuntimeError) as error:
            app.logger.exception("Team content save failed: %s", error)
            flash("Could not save team members. Check that the JSON is a valid array.", "error")
        return redirect("/site-content/team")

    content = get_site_content()
    return render_template(
        "team_content.html",
        team_json=json.dumps(content.get("team", DEFAULT_SITE_CONTENT["team"]), indent=2, ensure_ascii=False),
        team=content.get("team", DEFAULT_SITE_CONTENT["team"]),
    )


@app.route("/api/blogs", methods=["GET"])
@no_cache
def public_blogs():
    collection = client["globsoft_db"]["blogs"]
    documents = collection.find({"status": "published"}).sort([
        ("published_at", -1),
        ("created_at", -1),
        ("_id", -1),
    ])
    return jsonify([get_public_blog(document) for document in documents])


@app.route("/api/blogs/<slug>", methods=["GET"])
@no_cache
def public_blog(slug):
    collection = client["globsoft_db"]["blogs"]
    # Atomically increment views count on reading the article
    document = collection.find_one_and_update(
        {"slug": slug, "status": "published"},
        {"$inc": {"views": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if not document:
        return jsonify({"success": False, "error": "Blog post not found"}), 404
    return jsonify(get_public_blog(document))

@app.route("/api/blog-preview/<slug>", methods=["GET"])
@no_cache
def blog_social_preview(slug):
    """
    Renders clean Open Graph and Twitter Card HTML metadata for social media scrapers
    and search bots, while instantly redirecting real users to the SPA blog URL.
    """
    db = client["globsoft_db"]
    blog = db["blogs"].find_one({"slug": slug, "status": "published"}) or db["blogs"].find_one({"slug": slug})
    
    frontend_base = os.getenv("FRONTEND_URL", "https://globsoft.vercel.app").rstrip("/")
    admin_base = os.getenv("ADMIN_PORTAL_URL", "https://adminglobsoft.vercel.app").rstrip("/")
    
    if not blog:
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Globsoft Tech | Your Vision, Our Code!</title>
    <meta property="og:title" content="Globsoft Tech | Your Vision, Our Code!">
    <meta property="og:description" content="We empower businesses with Smart & Affordable Tech Solutions without breaking your bank account!">
    <meta property="og:image" content="{frontend_base}/social-preview.jpg">
    <meta property="og:url" content="{frontend_base}/blog">
    <meta property="og:type" content="website">
    <meta name="twitter:card" content="summary_large_image">
    <script>
        if (!/bot|crawl|spider|slurp|googlebot|bingbot|duckduckbot|facebookexternalhit|meta-externalagent|facebot/i.test(navigator.userAgent)) {{
            window.location.replace("{frontend_base}/blog");
        }}
    </script>
</head>
<body></body>
</html>""", 200, {"Content-Type": "text/html; charset=utf-8"}

    title = html.escape(str(blog.get("seo_title") or blog.get("title") or "Globsoft Tech Blog"))
    desc = html.escape(str(blog.get("seo_description") or blog.get("excerpt") or "Read the latest article from Globsoft Tech."))
    blog_url = f"{frontend_base}/blog/{slug}"
    
    # Determine absolute image URL
    image_url = f"{frontend_base}/social-preview.jpg"
    if blog.get("image_data"):
        image_url = f"{admin_base}/api/blog-images/{blog['_id']}"
    elif blog.get("image_url"):
        img = str(blog["image_url"]).strip()
        if img.startswith("http://") or img.startswith("https://"):
            image_url = img
        elif img.startswith("/"):
            image_url = f"{admin_base}{img}"

    author_name = html.escape(str(blog.get("author") or "Globsoft Team"))

    html_content = f"""<!DOCTYPE html>
<html lang="en" prefix="og: https://ogp.me/ns#">
<head>
    <meta charset="UTF-8">
    <title>{title} | Globsoft Tech</title>
    <meta name="description" content="{desc}">
    <meta name="author" content="{author_name}">
    <link rel="canonical" href="{blog_url}">

    <!-- Open Graph / Facebook / Instagram / WhatsApp / Messenger -->
    <meta property="og:type" content="article">
    <meta property="og:site_name" content="Globsoft Tech">
    <meta property="og:title" content="{title}">
    <meta property="og:description" content="{desc}">
    <meta property="og:image" content="{image_url}">
    <meta property="og:image:secure_url" content="{image_url}">
    <meta property="og:image:width" content="1200">
    <meta property="og:image:height" content="630">
    <meta property="og:url" content="{blog_url}">
    <meta property="og:locale" content="en_US">
    <meta property="article:author" content="{author_name}">

    <!-- Twitter / X -->
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="{title}">
    <meta name="twitter:description" content="{desc}">
    <meta name="twitter:image" content="{image_url}">

    <!-- Conditional redirect: Real users get redirected instantly, search bots & scrapers stay to read metadata -->
    <script>
        if (!/bot|crawl|spider|slurp|googlebot|bingbot|duckduckbot|facebookexternalhit|meta-externalagent|facebot/i.test(navigator.userAgent)) {{
            window.location.replace("{blog_url}");
        }}
    </script>
</head>
<body style="background:#0e0c15;color:#fff;font-family:sans-serif;padding:2rem;">
    <h2>{title}</h2>
    <p>{desc}</p>
    <p><a href="{blog_url}">Click here to view full article on Globsoft Tech</a></p>
</body>
</html>"""
    return html_content, 200, {"Content-Type": "text/html; charset=utf-8"}
    
@app.route("/api/blogs/<slug>/related", methods=["GET"])
@no_cache
def related_blogs(slug):
    """Return up to 3 related or recent published posts."""
    collection = client["globsoft_db"]["blogs"]
    current = collection.find_one({"slug": slug, "status": "published"})
    category = current.get("category") if current else None

    query = {"slug": {"$ne": slug}, "status": "published"}
    related = []
    if category:
        related = list(collection.find({**query, "category": category}).sort("published_at", -1).limit(3))
    if len(related) < 3:
        existing_ids = [doc["_id"] for doc in related]
        fallback = list(collection.find({**query, "_id": {"$nin": existing_ids}}).sort("published_at", -1).limit(3 - len(related)))
        related.extend(fallback)
    return jsonify([get_public_blog(doc) for doc in related])


@app.route("/api/blogs/<slug>/comments", methods=["POST"])
def post_blog_comment(slug):
    """Allow readers to submit comments to a published blog post."""
    try:
        data = request.get_json(silent=True) or request.form
        author_name = str(data.get("author_name", "")).strip()
        comment_text = str(data.get("content", "")).strip()

        if not author_name or len(author_name) > 60:
            return jsonify({"success": False, "error": "Name is required (max 60 characters)."}), 400
        if not comment_text or len(comment_text) > 1500:
            return jsonify({"success": False, "error": "Comment text is required (max 1500 characters)."}), 400

        comment_doc = {
            "id": str(ObjectId()),
            "author_name": author_name,
            "content": comment_text,
            "created_at": datetime.utcnow().isoformat(),
        }
        res = client["globsoft_db"]["blogs"].update_one(
            {"slug": slug, "status": "published"},
            {"$push": {"comments": comment_doc}},
        )
        if res.matched_count == 0:
            return jsonify({"success": False, "error": "Blog post not found."}), 404
        return jsonify({"success": True, "comment": comment_doc}), 201
    except Exception:
        app.logger.exception("Comment submission error")
        return jsonify({"success": False, "error": "Unable to save comment."}), 500


@app.route("/api/blog-images/<blog_id>", methods=["GET", "HEAD"])
def blog_image(blog_id):
    try:
        document = client["globsoft_db"]["blogs"].find_one(
            {"_id": ObjectId(blog_id)},
            {"image_data": 1, "image_mime": 1},
        )
    except Exception:
        return jsonify({"success": False, "error": "Invalid image"}), 400
    if not document or not document.get("image_data"):
        return jsonify({"success": False, "error": "Image not found"}), 404
    data_bytes = bytes(document["image_data"])
    resp = Response(data_bytes, mimetype=document.get("image_mime", "image/jpeg"))
    resp.headers['Content-Length'] = str(len(data_bytes))
    resp.headers['Accept-Ranges'] = 'bytes'
    resp.headers['Cache-Control'] = 'public, max-age=86400, immutable'
    return resp


@app.route("/api/blog-media/<media_id>", methods=["GET", "HEAD"])
def blog_media(media_id):
    try:
        doc = client["globsoft_db"]["blog_media"].find_one({"_id": ObjectId(media_id)})
    except Exception:
        return jsonify({"success": False, "error": "Invalid image ID"}), 400
    if not doc or not doc.get("data"):
        return jsonify({"success": False, "error": "Image not found"}), 404
    data_bytes = bytes(doc["data"])
    resp = Response(data_bytes, mimetype=doc.get("mimetype", "image/jpeg"))
    resp.headers['Content-Length'] = str(len(data_bytes))
    resp.headers['Accept-Ranges'] = 'bytes'
    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp


@app.route("/api/blog-images/upload", methods=["POST"])
def upload_blog_image():
    """Upload an image from the blog editor and store directly in MongoDB."""
    if not can_manage_blogs():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    file = request.files.get("image") or request.files.get("file")
    if not file or not file.filename:
        return jsonify({"success": False, "error": "No image file provided."}), 400

    data = file.read()
    if len(data) > BLOG_IMAGE_MAX_BYTES:
        return jsonify({"success": False, "error": f"Image must be {BLOG_IMAGE_MAX_BYTES // (1024 * 1024)} MB or smaller."}), 400

    mime = normalize_image_mime(file.mimetype, file.filename)
    if not mime:
        return jsonify({"success": False, "error": "Please upload a valid JPG, PNG, WEBP, GIF, or SVG image."}), 400

    media_doc = {
        "filename": file.filename,
        "data": data,
        "mimetype": mime,
        "size": len(data),
        "created_at": datetime.utcnow(),
        "uploaded_by": session.get("user"),
    }
    result = client["globsoft_db"]["blog_media"].insert_one(media_doc)
    media_url = f"/api/blog-media/{result.inserted_id}"
    return jsonify({
        "success": True,
        "url": media_url,
        "id": str(result.inserted_id),
    })


def normalize_image_mime(mimetype, filename=""):
    m = (mimetype or "").lower().strip()
    if m in {"image/jpeg", "image/jpg", "image/pjpeg"}:
        return "image/jpeg"
    if m in {"image/png", "image/x-png"}:
        return "image/png"
    if m in {"image/webp", "image/gif", "image/svg+xml"}:
        return m
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    if ext == ".svg":
        return "image/svg+xml"
    return None


def is_video_mime(mimetype, filename=""):
    """Check if the mimetype or file extension represents a supported video file."""
    m = (mimetype or "").lower().strip()
    if m.startswith("video/"):
        return True
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".3gp"}


def normalize_media_mime(mimetype, filename=""):
    """Normalize mimetype for either video or image."""
    m = (mimetype or "").lower().strip()
    ext = os.path.splitext(filename or "")[1].lower()
    if m.startswith("video/") or ext in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}:
        if ext == ".mov" or m == "video/quicktime":
            return "video/quicktime"
        if ext == ".webm" or m == "video/webm":
            return "video/webm"
        return "video/mp4"
    return normalize_image_mime(mimetype, filename)


def extract_and_store_base64_images(html_content):
    """
    Extract any pasted base64 data URLs from HTML content, save binary to MongoDB blog_media,
    and replace with clean /api/blog-media/<id> URLs.
    """
    if not html_content or "data:image/" not in html_content:
        return html_content

    pattern = re.compile(r'src=["\']data:(image\/[a-zA-Z0-9\+\-\.]+);base64,([A-Za-z0-9+/=]+)["\']')

    def replacer(match):
        mime = normalize_image_mime(match.group(1)) or "image/jpeg"
        b64data = match.group(2)
        try:
            raw_bytes = base64.b64decode(b64data)
            if len(raw_bytes) > BLOG_IMAGE_MAX_BYTES:
                return match.group(0)
            media_doc = {
                "filename": f"pasted-{int(time.time() * 1000)}",
                "data": raw_bytes,
                "mimetype": mime,
                "size": len(raw_bytes),
                "created_at": datetime.utcnow(),
                "uploaded_by": session.get("user"),
            }
            res = client["globsoft_db"]["blog_media"].insert_one(media_doc)
            return f'src="/api/blog-media/{res.inserted_id}"'
        except Exception:
            return match.group(0)

    return pattern.sub(replacer, html_content)


def blog_form_values():
    title = request.form.get("title", "").strip()
    slug = slugify(request.form.get("slug", "").strip() or title)
    excerpt = request.form.get("excerpt", "").strip()
    content = request.form.get("content", "").strip()
    author_avatar = None
    if not is_admin():
        author = session.get("author_name", "Author").strip()
        author_email = (session.get("author_email") or session.get("user") or "").strip().lower()
        author_id = session.get("author_id")
        author_avatar = session.get("avatar")
        if author_id and not author_avatar:
            try:
                auth_doc = client["globsoft_db"]["authors"].find_one({"_id": ObjectId(author_id)})
                if auth_doc:
                    author_avatar = auth_doc.get("avatar_url")
            except Exception:
                pass
    else:
        # Admin selects author by UNIQUE EMAIL or UNIQUE AUTHOR_ID
        selected_author_email = request.form.get("author_email", "").strip().lower()
        selected_author_id = request.form.get("author_id", "").strip()
        selected_author_name = request.form.get("author", "").strip()

        auth_doc = None
        if selected_author_email:
            auth_doc = client["globsoft_db"]["authors"].find_one({"email": selected_author_email})
        elif selected_author_id:
            try:
                auth_doc = client["globsoft_db"]["authors"].find_one({"_id": ObjectId(selected_author_id)})
            except Exception:
                pass

        if auth_doc:
            author = auth_doc.get("name") or selected_author_name or "Globsoft Team"
            author_email = auth_doc.get("email")
            author_id = str(auth_doc["_id"])
            author_avatar = auth_doc.get("avatar_url")
        else:
            author = selected_author_name or session.get("admin_name", "Globsoft Admin")
            author_email = (session.get("user") or admin_email).strip().lower()
            author_id = None
            author_avatar = session.get("avatar")

    category = request.form.get("category", "Technology").strip() or "Technology"
    tags = request.form.get("tags", "").strip()
    status = request.form.get("status", "draft").strip().lower()
    seo_title = request.form.get("seo_title", "").strip()
    seo_description = request.form.get("seo_description", "").strip()
    if not title or not excerpt or not content or not author:
        raise ValueError("Title, excerpt, content, and author are required.")
    if status not in {"draft", "published"}:
        raise ValueError("Invalid blog status.")
    if len(title) > 180 or len(excerpt) > 320:
        raise ValueError("Title or excerpt is too long.")
    # Extract any pasted base64 images and store in MongoDB
    content = extract_and_store_base64_images(content)
    if len(content) > 5000000:
        raise ValueError("Article content is too long.")
    read_time = calculate_read_time(content)

    return {
        "title": title,
        "slug": slug,
        "excerpt": excerpt,
        "content": content,
        "author": author,
        "author_email": author_email,
        "author_id": author_id,
        "author_avatar": author_avatar,
        "category": category,
        "tags": tags,
        "read_time": read_time,
        "status": status,
        "seo_title": seo_title[:180],
        "seo_description": seo_description[:320],
    }


def apply_blog_image(document):
    image = request.files.get("cover_image")
    cover_image_url = request.form.get("cover_image_url", "").strip()
    if request.form.get("remove_cover_image") == "1":
        document["image_data"] = None
        document["image_mime"] = None
        document["image_url"] = None
        return

    if image and image.filename:
        image_data = image.read()
        if len(image_data) > BLOG_IMAGE_MAX_BYTES:
            raise ValueError(f"Cover image must be {BLOG_IMAGE_MAX_BYTES // (1024 * 1024)} MB or smaller.")
        mime = normalize_image_mime(image.mimetype, image.filename)
        if not mime:
            raise ValueError("Use a JPG, PNG, WEBP, GIF, or SVG cover image.")
        document["image_data"] = image_data
        document["image_mime"] = mime
        document["image_url"] = None
    elif cover_image_url:
        document["image_url"] = cover_image_url
        document["image_data"] = None
        document["image_mime"] = None


def get_authors_for_blog_form():
    if is_admin():
        admin_entry = {
            "_id": "",
            "name": session.get("admin_name") or "Globsoft Administrator",
            "email": admin_email,
        }
        approved = list(client["globsoft_db"]["authors"].find({"active": True}).sort("name", 1))
        return [admin_entry] + approved
    return [{
        "_id": str(session.get("author_id", "")),
        "name": session.get("author_name"),
        "email": session.get("author_email") or session.get("user"),
    }]


@app.route("/blogs", methods=["GET"])
@no_cache
def manage_blogs():
    if not can_manage_blogs():
        return redirect("/pending-approval" if session.get("pending_user") else "/")

    if is_admin():
        documents = list(client["globsoft_db"]["blogs"].find().sort("updated_at", -1))
        total_posts = len(documents)
        published_count = sum(1 for b in documents if b.get("status") == "published")
        drafts_count = total_posts - published_count
        total_views = sum(int(b.get("views", 0)) for b in documents)
        total_comments = sum(len(b.get("comments", [])) for b in documents)
        authors_count = client["globsoft_db"]["authors"].count_documents({"active": True})
        stats = {
            "total": total_posts,
            "published": published_count,
            "drafts": drafts_count,
            "views": total_views,
            "comments": total_comments,
            "authors": authors_count,
        }
    else:
        # AUTHOR PRIVILEGE ISOLATION: author can ONLY see their own posts by EMAIL or ID!
        u_email = (session.get("user") or session.get("author_email") or "").strip().lower()
        a_id = str(session.get("author_id", ""))
        query_filter = {
            "$or": [
                {"author_email": u_email},
                {"created_by": u_email},
                {"author_id": a_id},
            ]
        }
        documents = list(client["globsoft_db"]["blogs"].find(query_filter).sort("updated_at", -1))
        total_posts = len(documents)
        published_count = sum(1 for b in documents if b.get("status") == "published")
        drafts_count = total_posts - published_count
        total_views = sum(int(b.get("views", 0)) for b in documents)
        total_comments = sum(len(b.get("comments", [])) for b in documents)
        stats = {
            "total": total_posts,
            "published": published_count,
            "drafts": drafts_count,
            "views": total_views,
            "comments": total_comments,
            "authors": 1,
        }

    return render_template("blogs.html", blogs=documents, is_admin=is_admin(), stats=stats)


@app.route("/blogs/<blog_id>/comments/<comment_id>/delete", methods=["POST"])
@no_cache
def delete_blog_comment(blog_id, comment_id):
    if not can_manage_blogs():
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json or "application/json" in request.headers.get("Accept", ""):
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return redirect("/")
    try:
        object_id = ObjectId(blog_id)
        blog = client["globsoft_db"]["blogs"].find_one({"_id": object_id})
        if not blog:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json or "application/json" in request.headers.get("Accept", ""):
                return jsonify({"success": False, "error": "Blog not found"}), 404
            return redirect("/blogs")

        # Authors can only moderate comments on their own blogs
        if not is_admin():
            u_email = (session.get("user") or session.get("author_email") or "").strip().lower()
            a_id = str(session.get("author_id", ""))
            is_owner = (
                blog.get("created_by") == u_email
                or blog.get("author_email") == u_email
                or (a_id and str(blog.get("author_id", "")) == a_id)
            )
            if not is_owner:
                if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json or "application/json" in request.headers.get("Accept", ""):
                    return jsonify({"success": False, "error": "Unauthorized"}), 403
                return redirect("/blogs")

        client["globsoft_db"]["blogs"].update_one(
            {"_id": object_id},
            {"$pull": {"comments": {"id": comment_id}}}
        )
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json or "application/json" in request.headers.get("Accept", ""):
            return jsonify({"success": True})
        flash("Comment removed.", "success")
    except Exception:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json or "application/json" in request.headers.get("Accept", ""):
            return jsonify({"success": False, "error": "Could not remove comment."}), 500
        flash("Could not remove comment.", "error")
    return redirect(f"/blogs/{blog_id}/edit")


@app.route("/blogs/new", methods=["GET", "POST"])
@no_cache
def create_blog():
    if not can_manage_blogs():
        return redirect("/pending-approval" if session.get("pending_user") else "/")
    if request.method == "POST":
        try:
            values = blog_form_values()
            if client["globsoft_db"]["blogs"].find_one({"slug": values["slug"]}):
                raise ValueError("That slug is already in use.")
            values["created_at"] = datetime.utcnow()
            values["updated_at"] = datetime.utcnow()
            values["published_at"] = datetime.utcnow() if values["status"] == "published" else None
            values["created_by"] = (session.get("user") or session.get("author_email") or "").strip().lower()
            if not values.get("author_email"):
                values["author_email"] = values["created_by"]
            apply_blog_image(values)
            client["globsoft_db"]["blogs"].insert_one(values)
            flash("Blog post created successfully.", "success")
            return redirect("/blogs")
        except (ValueError, RuntimeError) as error:
            flash(str(error), "error")
            authors = get_authors_for_blog_form()
            return render_template("blog_form.html", blog=request.form, authors=authors, is_admin=is_admin(), admin_email=admin_email)
    authors = get_authors_for_blog_form()
    return render_template("blog_form.html", blog=None, authors=authors, is_admin=is_admin(), admin_email=admin_email)


@app.route("/blogs/<blog_id>/edit", methods=["GET", "POST"])
@no_cache
def edit_blog(blog_id):
    if not can_manage_blogs():
        return redirect("/pending-approval" if session.get("pending_user") else "/")
    try:
        object_id = ObjectId(blog_id)
    except Exception:
        return redirect("/blogs")
    collection = client["globsoft_db"]["blogs"]
    blog = collection.find_one({"_id": object_id})
    if not blog:
        return redirect("/blogs")

    # Author ownership check: authors can only edit their own posts
    if not is_admin():
        u_email = (session.get("user") or session.get("author_email") or "").strip().lower()
        a_id = str(session.get("author_id", ""))
        is_owner = (
            (blog.get("created_by") or "").strip().lower() == u_email
            or (blog.get("author_email") or "").strip().lower() == u_email
            or (a_id and str(blog.get("author_id", "")) == a_id)
        )
        if not is_owner:
            flash("You are only permitted to edit your own blog articles.", "error")
            return redirect("/blogs")

    if request.method == "POST":
        try:
            values = blog_form_values()
            duplicate = collection.find_one({"slug": values["slug"], "_id": {"$ne": object_id}})
            if duplicate:
                raise ValueError("That slug is already in use.")
            values["updated_at"] = datetime.utcnow()
            if values["status"] == "published" and not blog.get("published_at"):
                values["published_at"] = datetime.utcnow()
            else:
                values["published_at"] = blog.get("published_at")
            if not values.get("author_email") and blog.get("author_email"):
                values["author_email"] = blog["author_email"]
            apply_blog_image(values)
            collection.update_one({"_id": object_id}, {"$set": values})
            flash("Blog post updated successfully.", "success")
            return redirect("/blogs")
        except (ValueError, RuntimeError) as error:
            flash(str(error), "error")
            authors = get_authors_for_blog_form()
            merged = {**blog, **request.form}
            return render_template("blog_form.html", blog=merged, authors=authors, is_admin=is_admin(), admin_email=admin_email)
    authors = get_authors_for_blog_form()
    return render_template("blog_form.html", blog=blog, authors=authors, is_admin=is_admin(), admin_email=admin_email)


@app.route("/blogs/<blog_id>/delete", methods=["POST"])
@no_cache
def delete_blog(blog_id):
    if not can_manage_blogs():
        return redirect("/blogs")
    try:
        object_id = ObjectId(blog_id)
        collection = client["globsoft_db"]["blogs"]
        blog = collection.find_one({"_id": object_id})
        if not blog:
            return redirect("/blogs")
        if not is_admin():
            u_email = (session.get("user") or session.get("author_email") or "").strip().lower()
            a_id = str(session.get("author_id", ""))
            is_owner = (
                (blog.get("created_by") or "").strip().lower() == u_email
                or (blog.get("author_email") or "").strip().lower() == u_email
                or (a_id and str(blog.get("author_id", "")) == a_id)
            )
            if not is_owner:
                flash("You are only permitted to delete your own blog articles.", "error")
                return redirect("/blogs")
        collection.delete_one({"_id": object_id})
        flash("Blog post deleted.", "success")
    except Exception:
        flash("Could not delete that blog post.", "error")
    return redirect("/blogs")


def get_author_auto_approve_setting():
    try:
        doc = client["globsoft_db"]["settings"].find_one({"key": "author_settings"})
        if doc and "auto_approve" in doc:
            return bool(doc["auto_approve"])
    except Exception:
        pass
    return False


def set_author_auto_approve_setting(enabled):
    try:
        client["globsoft_db"]["settings"].update_one(
            {"key": "author_settings"},
            {"$set": {"auto_approve": bool(enabled), "updated_at": datetime.utcnow()}},
            upsert=True
        )
        return True
    except Exception:
        return False

@app.route("/authors", methods=["GET", "POST"])
@no_cache
def manage_authors():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")
    collection = client["globsoft_db"]["authors"]
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not is_valid_email(email) or len(password) < 8:
            flash("Add a name, valid email, and password of at least 8 characters.", "error")
        elif collection.find_one({"email": email}):
            flash("That author email already exists.", "error")
        else:
            collection.insert_one({
                "name": name,
                "email": email,
                "password_hash": generate_password_hash(password),
                "active": True,
                "created_at": datetime.utcnow(),
                "last_login_at": datetime.utcnow(),
            })
            flash("Author account created.", "success")
        return redirect("/authors")

    # --- STEP 3 REPLACES THE OLD `authors = list(...)` LINE HERE ---
    all_docs = list(collection.find({}, {"password_hash": 0}).sort([("active", -1), ("created_at", -1)]))
    seen_emails = set()
    authors = []
    for doc in all_docs:
        a_email = (doc.get("email") or "").strip().lower()
        if a_email and a_email in seen_emails:
            # Automatically delete duplicate record (active/approved record is kept first)
            collection.delete_one({"_id": doc["_id"]})
            continue
        if a_email:
            seen_emails.add(a_email)
        authors.append(doc)
    # ---------------------------------------------------------------

    for a in authors:
        a_id = str(a["_id"])
        a_email = (a.get("email") or "").strip().lower()
        a["post_count"] = client["globsoft_db"]["blogs"].count_documents({
            "$or": [
                {"author_id": a_id},
                {"created_by": a_email},
                {"author_email": a_email},
            ]
        })

    total_authors = len(authors)
    active_authors = sum(1 for a in authors if a.get("active"))
    pending_authors = total_authors - active_authors
    stats = {
        "total": total_authors,
        "active": active_authors,
        "pending": pending_authors,
    }
    auto_approve = get_author_auto_approve_setting()
    return render_template("authors.html", authors=authors, stats=stats, auto_approve=auto_approve)
@app.route("/authors/toggle-auto-approve", methods=["POST"])
@no_cache
def toggle_author_auto_approve():
    if not is_admin():
        return redirect("/")
    current = get_author_auto_approve_setting()
    new_val = not current
    set_author_auto_approve_setting(new_val)
    status_txt = "turned ON (new writers will be automatically approved upon sign-in)" if new_val else "turned OFF (new writers will require manual admin approval)"
    flash(f"Author Auto-Approval {status_txt}.", "success")
    return redirect("/authors")

@app.route("/authors/<author_id>/approve", methods=["POST"])
@no_cache
def approve_author(author_id):
    if not is_admin():
        return redirect("/")
    try:
        client["globsoft_db"]["authors"].update_one(
            {"_id": ObjectId(author_id)},
            {"$set": {"active": True, "approved_at": datetime.utcnow()}}
        )
        flash("Author approved successfully! They can now log in and write blogs.", "success")
    except Exception:
        flash("Could not approve author.", "error")
    return redirect("/authors")


@app.route("/authors/<author_id>/toggle", methods=["POST"])
@no_cache
def toggle_author(author_id):
    if not is_admin():
        return redirect("/")
    try:
        collection = client["globsoft_db"]["authors"]
        author = collection.find_one({"_id": ObjectId(author_id)})
        if author:
            new_status = not author.get("active", False)
            collection.update_one({"_id": author["_id"]}, {"$set": {"active": new_status}})
            action_name = "approved and activated" if new_status else "suspended"
            flash(f"Author {author.get('name')} {action_name}.", "success")
    except Exception:
        flash("Could not update author status.", "error")
    return redirect("/authors")


@app.route("/authors/<author_id>/toggle-team", methods=["POST"])
@no_cache
def toggle_author_team(author_id):
    if not is_admin():
        return redirect("/")
    try:
        collection = client["globsoft_db"]["authors"]
        author = collection.find_one({"_id": ObjectId(author_id)})
        if author:
            # show_on_team defaults to True unless explicitly False
            current_status = author.get("show_on_team", True)
            new_status = not current_status
            collection.update_one({"_id": author["_id"]}, {"$set": {"show_on_team": new_status}})
            action = "visible on Our Team page" if new_status else "removed from Our Team page"
            flash(f"Author {author.get('name')} is now {action}.", "success")
    except Exception:
        flash("Could not update author team visibility.", "error")
    return redirect("/authors")


@app.route("/authors/<author_id>/delete", methods=["POST"])
@no_cache
def delete_author(author_id):
    if not is_admin():
        return redirect("/")
    try:
        client["globsoft_db"]["authors"].delete_one({"_id": ObjectId(author_id)})
        flash("Author account removed.", "success")
    except Exception:
        flash("Could not delete author account.", "error")
    return redirect("/authors")


@app.route("/author/profile", methods=["GET", "POST"])
@no_cache
def author_profile():
    if not is_blog_author():
        return redirect("/")

    author_id = session.get("author_id")
    authors_col = client["globsoft_db"]["authors"]
    author = authors_col.find_one({"_id": ObjectId(author_id)}) if author_id else None

    if not author and is_admin():
        author = {
            "name": session.get("admin_name") or "Globsoft Administrator",
            "email": session.get("user"),
            "google_name": "Globsoft Tech",
            "avatar_url": session.get("avatar"),
            "active": True,
        }
    elif not author:
        flash("Author profile not found.", "error")
        return redirect("/blogs")

    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        bio = request.form.get("bio", "").strip()
        public_email = request.form.get("public_email", "").strip().lower()
        role = request.form.get("role", "").strip()
        socials = {
            "instagram": request.form.get("social_instagram", "").strip(),
            "linkedin": request.form.get("social_linkedin", "").strip(),
            "github": request.form.get("social_github", "").strip(),
            "twitter": request.form.get("social_twitter", "").strip(),
        }

        if not new_name or len(new_name) > 60:
            flash("Display name must be between 1 and 60 characters.", "error")
        else:
            old_name = author.get("name")
            if author.get("_id"):
                update_fields = {
                    "name": new_name,
                    "bio": bio,
                    "public_email": public_email,
                    "role": role,
                    "socials": socials,
                    "updated_at": datetime.utcnow(),
                }
                authors_col.update_one(
                    {"_id": author["_id"]},
                    {"$set": update_fields}
                )
            session["author_name"] = new_name
            a_email = (author.get("email") or session.get("author_email") or session.get("user") or "").strip().lower()
            a_id = str(author.get("_id", ""))
            # Update all articles authored by this specific author with their new name (strictly by email / id)
            client["globsoft_db"]["blogs"].update_many(
                {
                    "$or": [
                        {"author_id": a_id},
                        {"author_email": a_email},
                        {"created_by": a_email},
                    ]
                },
                {"$set": {"author": new_name}}
            )
            flash("Profile updated! Your new details and social links have been saved.", "success")
            return redirect("/author/profile")

    return render_template("author_profile.html", author=author, is_admin=is_admin())


@app.route("/pending-approval", methods=["GET"])
@no_cache
def pending_approval():
    if is_admin():
        return redirect("/")
    if session.get("role") == "author":
        return redirect("/")

    email = session.get("pending_user") or session.get("user")
    if not email:
        return redirect("/")

    # Check if admin has approved this author in MongoDB
    author = client["globsoft_db"]["authors"].find_one({"email": email})
    if author and author.get("active"):
        # Approved! Promote session to active author
        session.clear()
        session["user"] = email
        session["role"] = "author"
        session["author_id"] = str(author["_id"])
        session["author_name"] = author.get("name")
        session["author_email"] = email
        session["avatar"] = author.get("avatar_url")
        flash("Congratulations! Your author account has been approved by the administrator.", "success")
        return redirect("/")

    return render_template(
        "pending_approval.html",
        email=email,
        name=session.get("pending_name") or (author.get("name") if author else "Author"),
        avatar=session.get("avatar") or (author.get("avatar_url") if author else None),
        clerk_publishable_key=clerk_publishable_key,
    )


@app.route("/api/auth/clerk-session", methods=["POST"])
def clerk_session_auth():
    """
    Authenticate a user via Clerk (Google OAuth or email).
    If email matches info.globsoft@gmail.com, grant default admin privileges.
    Otherwise, atomically register/update the author in MongoDB and check approval status.
    """
    try:
        data = request.get_json(silent=True) or {}
        email = str(data.get("email", "")).strip().lower()
        name = str(data.get("name", "")).strip()
        image_url = str(data.get("image_url", "")).strip()
        clerk_id = str(data.get("clerk_id", "")).strip()

        if not email or not is_valid_email(email):
            return jsonify({"success": False, "error": "Valid email is required from Google/Clerk."}), 400

        # Check default admin: info.globsoft@gmail.com or configured MONGO_USERNAME
        if email == admin_email or email == (usr_name or "").strip().lower():
            session.clear()
            session["user"] = email
            session["role"] = "admin"
            session["admin_name"] = name or "Globsoft Admin"
            session["avatar"] = image_url
            session.pop("author_id", None)
            return jsonify({"success": True, "redirect": "/"})

        # Non-admin author flow (Atomic Upsert prevents duplicate documents)
        authors_col = client["globsoft_db"]["authors"]
        now = datetime.utcnow()
        fallback_name = name or email.split("@")[0]
        auto_approve = get_author_auto_approve_setting()

        insert_active = True if auto_approve else False
        insert_approved_at = now if auto_approve else None

        author = authors_col.find_one_and_update(
            {"email": email},
            {
                "$set": {
                    "last_login_at": now,
                    "avatar_url": image_url,
                    "clerk_id": clerk_id,
                    "google_name": fallback_name,
                },
                "$setOnInsert": {
                    "email": email,
                    "name": fallback_name,
                    "active": insert_active,
                    "approved_at": insert_approved_at,
                    "role": "author",
                    "created_at": now,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

        # If auto-approve is active and this account was in pending status, activate immediately
        if auto_approve and not author.get("active", False):
            authors_col.update_one({"_id": author["_id"]}, {"$set": {"active": True, "approved_at": now}})
            author["active"] = True

        author_id = str(author["_id"])
        is_active = bool(author.get("active", False))
        display_name = author.get("name") or fallback_name
        avatar = image_url or author.get("avatar_url")

        if not is_active:
            # Awaiting admin approval
            session.clear()
            session["pending_user"] = email
            session["pending_name"] = display_name
            session["avatar"] = avatar
            return jsonify({"success": True, "redirect": "/pending-approval"})

        # Author is approved by admin!
        session.clear()
        session["user"] = email
        session["role"] = "author"
        session["author_id"] = author_id
        session["author_name"] = display_name
        session["author_email"] = email
        session["avatar"] = avatar
        return jsonify({"success": True, "redirect": "/"})
    except Exception:
        app.logger.exception("Clerk session auth error")
        return jsonify({"success": False, "error": "Authentication failed."}), 500

@app.route("/author-login", methods=["GET", "POST"])
@no_cache
def author_login():
    return redirect("/")


@app.route("/__clerk/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def clerk_proxy(subpath):
    """
    Reverse proxy for Clerk Frontend API on production domains.
    Preserves multiple Set-Cookie headers and dynamically matches origin.
    """
    if request.method == "OPTIONS":
        return Response("", status=200, headers={
            "Access-Control-Allow-Origin": request.headers.get("Origin", "*"),
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, Clerk-Secret-Key, Clerk-Proxy-Url",
            "Access-Control-Allow-Credentials": "true",
        })

    target_url = f"https://frontend-api.clerk.dev/{subpath}"
    if request.query_string:
        target_url += f"?{request.query_string.decode('utf-8')}"

    # Dynamically determine protocol and host (handles Vercel edge proxy headers)
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    dynamic_proxy_url = os.getenv("CLERK_PROXY_URL") or f"{proto}://{host}/__clerk"

    # Exclude hop-by-hop headers that conflict with upstream
    excluded_req_headers = {"host", "content-length", "accept-encoding", "connection"}
    fwd_headers = {k: v for k, v in request.headers if k.lower() not in excluded_req_headers}
    fwd_headers["Host"] = "frontend-api.clerk.dev"
    fwd_headers["Clerk-Proxy-Url"] = dynamic_proxy_url
    if clerk_secret_key:
        fwd_headers["Clerk-Secret-Key"] = clerk_secret_key
    fwd_headers["X-Forwarded-Host"] = host
    fwd_headers["X-Forwarded-Proto"] = proto
    fwd_headers["X-Forwarded-For"] = get_client_ip()

    excluded_resp_headers = {"content-encoding", "content-length", "transfer-encoding", "connection", "set-cookie"}

    if requests is not None:
        try:
            upstream_resp = requests.request(
                method=request.method,
                url=target_url,
                headers=fwd_headers,
                data=request.get_data() if request.method not in {"GET", "HEAD", "OPTIONS"} else None,
                allow_redirects=False,
                timeout=15,
            )
            resp = Response(upstream_resp.content, status=upstream_resp.status_code)
            for k, v in upstream_resp.headers.items():
                if k.lower() not in excluded_resp_headers:
                    resp.headers[k] = v

            # Preserve all individual Set-Cookie headers from urllib3 raw response
            if hasattr(upstream_resp, "raw") and getattr(upstream_resp.raw, "headers", None):
                raw_headers = upstream_resp.raw.headers
                cookies = raw_headers.getlist("Set-Cookie") if hasattr(raw_headers, "getlist") else []
                for cookie in cookies:
                    resp.headers.add("Set-Cookie", cookie)
            elif "Set-Cookie" in upstream_resp.headers:
                resp.headers.add("Set-Cookie", upstream_resp.headers["Set-Cookie"])

            return resp
        except Exception as e:
            app.logger.exception("Clerk proxy error")
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "The 'requests' library is required for Clerk proxy."}), 500


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
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")

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

# In app.py — Update the dashboard() route:
@app.route("/", methods=['GET', 'POST'])
@no_cache
def dashboard():
    if is_admin() or is_blog_author():
        return render_template('dashboard.html', is_admin=is_admin())

    if session.get("pending_user"):
        return redirect("/pending-approval")

    if request.method == "POST":
        username = (request.form.get("uname") or "").strip().lower()
        password = request.form.get("password")
        if (username in {admin_email, (usr_name or "").strip().lower()}) and password == usr_password:
            session.clear()
            session['user'] = admin_email
            session['role'] = 'admin'
            session['admin_name'] = "Globsoft Admin"
            return redirect("/")

        flash("Invalid administrator credentials.", "error")

    proxy_url = ""
    if clerk_publishable_key and clerk_publishable_key.startswith("pk_live_"):
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        host = request.headers.get("X-Forwarded-Host", request.host)
        proxy_url = os.getenv("CLERK_PROXY_URL") or f"{proto}://{host}/__clerk"
    return render_template(
        'login.html',
        clerk_publishable_key=clerk_publishable_key,
        clerk_frontend_api=clerk_frontend_api,
        clerk_proxy_url=proxy_url,
    )

@app.route("/logout", methods=['GET', 'POST'])
@no_cache
def logout():
    session.clear()
    return redirect("/?logged_out=1")

@app.route("/customer", methods=['GET', 'POST'])
@no_cache
def customer():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")
    db = client["customer_db"]
    collection = db["customers"]
    
    # Fetch all customers
    customers = list(collection.find().sort("date_added", -1))
    
    return render_template('customer.html', customers=customers)


@app.route("/customer/add", methods=['POST'])
@no_cache
def add_customer():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")
    db = client["customer_db"]
    collection = db["customers"]
    
    customer_name = request.form.get("customer_name")
    customer_location = request.form.get("customer_location")
    customer_phone = request.form.get("customer_phone")
    customer_email = request.form.get("customer_email", "").strip()
    customer_company = request.form.get("customer_company", "").strip()
    customer_service = request.form.get("customer_service", "").strip()
    customer_remark = request.form.get("customer_remark", "")
    
    # Check if customer with same phone already exists
    existing_customer = collection.find_one({"phone": customer_phone})
    
    if existing_customer:
        # Customer already exists - return with error message
        from flask import flash
        flash(f'Customer with phone number {customer_phone} already exists!', 'error')
        return redirect('/customer')
    
    customer_data = {
        "email": customer_email,
        "company": customer_company,
        "service": customer_service,
        "source": "Manual Entry",
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


@app.route("/customer/edit/<customer_id>", methods=['GET', 'POST'])
@no_cache
def edit_customer(customer_id):
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")
    db = client["customer_db"]
    collection = db["customers"]
    
    if request.method == 'POST':
        customer_name = request.form.get("customer_name")
        customer_location = request.form.get("customer_location")
        customer_phone = request.form.get("customer_phone")
        customer_email = request.form.get("customer_email", "").strip()
        customer_company = request.form.get("customer_company", "").strip()
        customer_service = request.form.get("customer_service", "").strip()
        customer_remark = request.form.get("customer_remark", "")
        
        updated_data = {
            "email": customer_email,
            "company": customer_company,
            "service": customer_service,
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


@app.route("/customer/delete", methods=['POST'])
@no_cache
def delete_customer():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")
    db = client["customer_db"]
    collection = db["customers"]
    
    customer_id = request.form.get("customer_id")
    collection.delete_one({"_id": ObjectId(customer_id)})
    return redirect('/customer')


@app.route("/customer/status", methods=['POST'])
@no_cache
def update_customer_status():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")
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

@app.route("/sanourl", methods=['GET', 'POST', 'DELETE'])
@no_cache
def sanourl():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")
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

@app.route("/newsletter", methods=['GET', 'POST', 'DELETE'])
@no_cache
def newsletter():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")
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
    
    return render_template(
        'newsletter.html',
        emails=emails,
        total_emails=total_emails,
        sender_email=os.getenv("GMAIL_SENDER", "info.globsoft@gmail.com"),
    )


@app.route("/newsletter/send", methods=["POST"])
@no_cache
def send_newsletter():
    if not is_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    subject = str(data.get("subject", "")).strip()
    plain_text = str(data.get("plain_text", "")).strip()
    html = str(data.get("html", "")).strip()
    preview = bool(data.get("preview", False))

    if not subject or len(subject) > 998:
        return jsonify({"success": False, "error": "Enter a subject up to 998 characters."}), 400
    if not plain_text and not html:
        return jsonify({"success": False, "error": "Add a plain-text or HTML message."}), 400

    sender, app_password = get_gmail_smtp_credentials()
    collection = client["emails_db"]["emails"]
    recipients = sorted({
        record.get("email", "").strip().lower()
        for record in collection.find({}, {"email": 1})
        if is_valid_email(str(record.get("email", "")).strip().lower())
    })
    if preview:
        recipients = [sender]
    if not recipients:
        return jsonify({"success": False, "error": "There are no valid newsletter subscribers."}), 400
    if not preview:
        allowed, daily_limit = reserve_daily_recipients(sender, len(recipients))
        if not allowed:
            return jsonify({
                "success": False,
                "error": (
                    f"This broadcast would exceed the configured daily limit of "
                    f"{daily_limit} recipients. Send a smaller campaign or continue tomorrow."
                ),
            }), 429

    try:
        smtp_connection = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        smtp_connection.login(sender, app_password)
        batch_size = 50
        sent = 0
        try:
            for start in range(0, len(recipients), batch_size):
                batch = recipients[start:start + batch_size]
                send_gmail_message(
                    smtp_connection,
                    sender,
                    batch,
                    subject,
                    plain_text,
                    html,
                    preview=preview,
                )
                sent += len(batch)
        finally:
            smtp_connection.quit()
        return jsonify({
            "success": True,
            "preview": preview,
            "sent": sent,
            "message": "Preview sent to the sender." if preview else f"Newsletter sent to {sent} subscriber(s).",
        })
    except Exception:
        app.logger.exception("Newsletter broadcast error")
        return jsonify({
            "success": False,
            "error": "Gmail could not send the newsletter. Check the SMTP credentials and Gmail sending quota.",
        }), 502

# Add this to your existing app.py file on the other domain

# ==================== GURUJI INSTRUCTOR VERIFICATION ====================

@app.route("/guruji-instructors", methods=['GET', 'POST'])
@no_cache
def guruji_instructors():
    """Manage Guruji instructor verification requests."""
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")
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


# =========================================================================
# SOCIAL MEDIA MANAGEMENT (DIRECT PUBLISHING TO FACEBOOK & INSTAGRAM)
# =========================================================================

def get_admin_base_url():
    """Public base URL of the Admin Portal backend (used by Meta/Instagram to fetch images)."""
    admin_env = (os.environ.get("ADMIN_PORTAL_URL") or os.environ.get("ADMIN_URL") or "").strip()
    if admin_env:
        return admin_env.rstrip("/")
    if has_request_context():
        proto = request.headers.get("X-Forwarded-Proto") or request.scheme or "https"
        host = request.headers.get("X-Forwarded-Host") or request.host
        if host:
            return f"{proto}://{host}".rstrip("/")
    return "https://adminglobsoft.vercel.app"


def get_site_base_url():
    """Public website URL for articles and pages."""
    return (os.environ.get("SITE_URL") or "https://globsoft.vercel.app").strip().rstrip("/")


def _get_meta_appsecret_proof(token, secret):
    """Compute HMAC-SHA256 appsecret_proof for Meta Graph API calls."""
    if not token or not secret:
        return None
    try:
        return hmac.new(
            secret.encode("utf-8"),
            token.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
    except Exception:
        return None


def _format_meta_error(res_json, default_msg="Meta API request failed"):
    """Extract clean, descriptive error messages from Meta Graph API responses."""
    if not isinstance(res_json, dict):
        return str(res_json)
    err = res_json.get("error")
    if isinstance(err, dict):
        msg = err.get("error_user_msg") or err.get("message") or default_msg
        code = err.get("code")
        subcode = err.get("error_subcode")
        err_type = err.get("type", "Meta")
        suffix = f" [Code: {code}" + (f", Subcode: {subcode}" if subcode else "") + "]" if code else ""
        return f"{err_type}: {msg}{suffix}"
    return res_json.get("message") or default_msg


def get_social_settings():
    """Fetch Facebook and Instagram credentials from MongoDB or environment variables."""
    doc = {}
    try:
        doc = client["globsoft_db"]["settings"].find_one({"key": "social_media"}) or {}
    except Exception:
        pass

    fb_page_id = (
        (doc.get("facebook_page_id") or "").strip()
        or os.environ.get("META_PAGE_ID", "").strip()
        or os.environ.get("FACEBOOK_PAGE_ID", "").strip()
    )
    fb_token = (
        (doc.get("facebook_access_token") or "").strip()
        or os.environ.get("META_PAGE_ACCESS_TOKEN", "").strip()
        or os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
    )
    ig_account_id = (
        (doc.get("instagram_account_id") or "").strip()
        or os.environ.get("META_IG_USER_ID", "").strip()
        or os.environ.get("INSTAGRAM_ACCOUNT_ID", "").strip()
    )
    app_id = (
        (doc.get("meta_app_id") or "").strip()
        or os.environ.get("META_APP_ID", "").strip()
    )
    app_secret = (
        (doc.get("meta_app_secret") or "").strip()
        or os.environ.get("META_APP_SECRET", "").strip()
    )
    default_hashtags = (
        (doc.get("default_hashtags") or "").strip()
        or "#GlobsoftTech #WebDevelopment #NepalTech #SoftwareCompany #TechSolutions"
    )

    fb_page_name = doc.get("facebook_page_name") or "Globsoft Tech Pvt. Ltd."
    ig_username = doc.get("instagram_username") or "globsoftech"

    appsecret_proof = _get_meta_appsecret_proof(fb_token, app_secret)

    return {
        "facebook_page_id": fb_page_id,
        "facebook_access_token": fb_token,
        "instagram_account_id": ig_account_id,
        "meta_app_id": app_id,
        "meta_app_secret": app_secret,
        "appsecret_proof": appsecret_proof,
        "default_hashtags": default_hashtags,
        "facebook_page_name": fb_page_name,
        "instagram_username": ig_username,
        "is_configured": bool(fb_page_id and fb_token),
        "env_page_id_present": bool(os.environ.get("META_PAGE_ID") or os.environ.get("FACEBOOK_PAGE_ID")),
        "env_token_present": bool(os.environ.get("META_PAGE_ACCESS_TOKEN") or os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN")),
        "env_ig_id_present": bool(os.environ.get("META_IG_USER_ID") or os.environ.get("INSTAGRAM_ACCOUNT_ID")),
        "env_app_id_present": bool(os.environ.get("META_APP_ID")),
        "env_app_secret_present": bool(os.environ.get("META_APP_SECRET")),
    }


def _meta_http_post(url, data=None, files=None, timeout=30):
    """POST helper with requests or urllib fallback, with network exception catching."""
    data = dict(data or {})
    if requests is not None:
        try:
            resp = requests.post(url, data=data, files=files, timeout=timeout)
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, {"error": {"message": resp.text}}
        except requests.exceptions.RequestException as e:
            return 500, {"error": {"message": f"Network error during POST request: {str(e)}"}}

    if files:
        return 400, {"error": {"message": "Binary image file upload requires the 'requests' package."}}

    try:
        encoded = urlencode(data).encode("utf-8")
        req = Request(url, data=encoded, headers={"User-Agent": "GlobsoftAdmin/1.0"})
        with urlopen(req, timeout=timeout) as r:
            return r.getcode(), json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        if hasattr(exc, "read"):
            try:
                return getattr(exc, "code", 400), json.loads(exc.read().decode("utf-8"))
            except Exception:
                pass
        return 500, {"error": {"message": str(exc)}}


def _meta_http_get(url, timeout=15):
    """GET helper with requests or urllib fallback, with network exception catching."""
    if requests is not None:
        try:
            resp = requests.get(url, timeout=timeout)
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, {"error": {"message": resp.text}}
        except requests.exceptions.RequestException as e:
            return 500, {"error": {"message": f"Network error during GET request: {str(e)}"}}

    try:
        req = Request(url, headers={"User-Agent": "GlobsoftAdmin/1.0"})
        with urlopen(req, timeout=timeout) as r:
            return r.getcode(), json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        if hasattr(exc, "read"):
            try:
                return getattr(exc, "code", 400), json.loads(exc.read().decode("utf-8"))
            except Exception:
                pass
        return 500, {"error": {"message": str(exc)}}


def post_to_facebook_page(
    caption,
    image_bytes=None,
    image_mime=None,
    image_url=None,
    video_bytes=None,
    video_mime=None,
    video_url=None,
    media_items=None,
    link_url=None,
    settings=None
):
    """
    Publish a post to Facebook Page via Meta Graph API.
    Supports:
    - Single video (multipart upload or public file URL)
    - Multi-photo album (multiple photos via attached_media)
    - Single photo (direct multipart upload or public image URL)
    - Status/link post
    """
    if not settings:
        settings = get_social_settings()

    page_id = settings.get("facebook_page_id")
    access_token = settings.get("facebook_access_token")
    proof = settings.get("appsecret_proof")

    if not page_id or not access_token:
        return {"success": False, "error": "Facebook Page ID or Page Access Token is missing."}

    graph_version = "v19.0"

    final_caption = caption
    if link_url and link_url not in final_caption:
        final_caption = f"{caption}\n\n🔗 {link_url}"

    try:
        # Case 1: Video Post
        if video_bytes or video_url:
            url = f"https://graph.facebook.com/{graph_version}/{page_id}/videos"
            data = {
                "description": final_caption,
                "access_token": access_token
            }
            if proof:
                data["appsecret_proof"] = proof

            files = None
            if video_bytes:
                files = {
                    "source": ("social_post_video.mp4", video_bytes, video_mime or "video/mp4")
                }
            elif video_url:
                data["file_url"] = video_url

            status_code, res_json = _meta_http_post(url, data=data, files=files, timeout=90)
            if status_code == 200 and ("id" in res_json):
                video_id = res_json["id"]
                return {
                    "success": True,
                    "post_id": str(video_id),
                    "url": f"https://www.facebook.com/{video_id}"
                }
            else:
                err_msg = _format_meta_error(res_json, "Facebook video upload failed")
                return {"success": False, "error": f"Facebook Error: {err_msg}"}

        # Case 2: Multi-Photo Post (2 or more photos)
        elif media_items and len(media_items) > 1:
            photo_ids = []
            for idx, item in enumerate(media_items):
                p_url = f"https://graph.facebook.com/{graph_version}/{page_id}/photos"
                p_data = {
                    "published": "false",
                    "temporary": "true",
                    "access_token": access_token
                }
                if proof:
                    p_data["appsecret_proof"] = proof

                p_files = None
                if item.get("bytes"):
                    p_files = {
                        "source": (item.get("filename", f"photo_{idx}.jpg"), item["bytes"], item.get("mime", "image/jpeg"))
                    }
                elif item.get("url"):
                    p_data["url"] = item["url"]
                else:
                    continue

                p_status, p_json = _meta_http_post(p_url, data=p_data, files=p_files, timeout=30)
                if p_status == 200 and "id" in p_json:
                    photo_ids.append(p_json["id"])
                else:
                    err_msg = _format_meta_error(p_json, f"Failed uploading photo {idx+1}")
                    return {"success": False, "error": f"Facebook Multi-Photo Error: {err_msg}"}

            if not photo_ids:
                return {"success": False, "error": "No valid photos could be prepared for multi-photo post."}

            feed_url = f"https://graph.facebook.com/{graph_version}/{page_id}/feed"
            feed_data = {
                "message": final_caption,
                "attached_media": json.dumps([{"media_fbid": pid} for pid in photo_ids]),
                "access_token": access_token
            }
            if proof:
                feed_data["appsecret_proof"] = proof

            feed_status, feed_json = _meta_http_post(feed_url, data=feed_data, timeout=30)
            if feed_status == 200 and "id" in feed_json:
                post_id = feed_json["id"]
                return {
                    "success": True,
                    "post_id": str(post_id),
                    "url": f"https://www.facebook.com/{post_id}"
                }
            else:
                err_msg = _format_meta_error(feed_json, "Facebook multi-photo publishing failed")
                return {"success": False, "error": f"Facebook Error: {err_msg}"}

        # Case 3: Single Image Multipart Binary
        elif image_bytes:
            url = f"https://graph.facebook.com/{graph_version}/{page_id}/photos"
            files = {
                "source": ("social_post_image.jpg", image_bytes, image_mime or "image/jpeg")
            }
            data = {
                "caption": final_caption,
                "access_token": access_token
            }
            if proof:
                data["appsecret_proof"] = proof
            status_code, res_json = _meta_http_post(url, data=data, files=files, timeout=30)
            if status_code == 200 and ("id" in res_json or "post_id" in res_json):
                post_id = res_json.get("post_id") or res_json.get("id")
                return {
                    "success": True,
                    "post_id": str(post_id),
                    "url": f"https://www.facebook.com/{post_id}"
                }
            else:
                err_msg = _format_meta_error(res_json, "Facebook photo upload failed")
                return {"success": False, "error": f"Facebook Error: {err_msg}"}

        # Case 4: Single Image Public URL
        elif image_url and (image_url.startswith("http://") or image_url.startswith("https://")):
            url = f"https://graph.facebook.com/{graph_version}/{page_id}/photos"
            data = {
                "url": image_url,
                "caption": final_caption,
                "access_token": access_token
            }
            if proof:
                data["appsecret_proof"] = proof
            status_code, res_json = _meta_http_post(url, data=data, timeout=30)
            if status_code == 200 and ("id" in res_json or "post_id" in res_json):
                post_id = res_json.get("post_id") or res_json.get("id")
                return {
                    "success": True,
                    "post_id": str(post_id),
                    "url": f"https://www.facebook.com/{post_id}"
                }
            else:
                err_msg = _format_meta_error(res_json, "Facebook photo post failed")
                return {"success": False, "error": f"Facebook Error: {err_msg}"}

        # Case 5: Text / Link Feed Post
        else:
            url = f"https://graph.facebook.com/{graph_version}/{page_id}/feed"
            data = {
                "message": caption,
                "access_token": access_token
            }
            if link_url:
                data["link"] = link_url
            if proof:
                data["appsecret_proof"] = proof
            status_code, res_json = _meta_http_post(url, data=data, timeout=30)
            if status_code == 200 and "id" in res_json:
                post_id = res_json["id"]
                return {
                    "success": True,
                    "post_id": str(post_id),
                    "url": f"https://www.facebook.com/{post_id}"
                }
            else:
                err_msg = _format_meta_error(res_json, "Facebook feed post failed")
                return {"success": False, "error": f"Facebook Error: {err_msg}"}

    except Exception as e:
        return {"success": False, "error": f"Facebook API Connection Error: {str(e)}"}


def post_to_instagram_business(caption, image_url=None, video_url=None, media_items=None, settings=None):
    """
    Publish to Instagram Business Account via Meta Graph API.
    Supports:
    - Single video / Reel (media_type=REELS)
    - Carousel (2 to 10 photos or videos, media_type=CAROUSEL)
    - Single photo
    """
    if not settings:
        settings = get_social_settings()

    ig_account_id = settings.get("instagram_account_id")
    access_token = settings.get("facebook_access_token")
    proof = settings.get("appsecret_proof")

    if not ig_account_id or not access_token:
        return {"success": False, "error": "Instagram Business Account ID or Access Token is missing."}

    graph_version = "v19.0"

    try:
        creation_id = None

        # Case 1: Video / Reel Post
        if video_url:
            if video_url.startswith("http://"):
                video_url = "https://" + video_url[len("http://"):]

            container_url = f"https://graph.facebook.com/{graph_version}/{ig_account_id}/media"
            c_data = {
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": access_token
            }
            if proof:
                c_data["appsecret_proof"] = proof

            c_status, c_json = _meta_http_post(container_url, data=c_data, timeout=30)
            if c_status != 200 or "id" not in c_json:
                err_msg = _format_meta_error(c_json, "Failed to create Instagram video container")
                return {"success": False, "error": f"Instagram Video Error: {err_msg}"}

            creation_id = c_json["id"]

            status_url = f"https://graph.facebook.com/{graph_version}/{creation_id}?fields=status_code,status&access_token={access_token}"
            if proof:
                status_url += f"&appsecret_proof={proof}"

            for _ in range(20):
                time.sleep(3)
                s_status, s_json = _meta_http_get(status_url, timeout=10)
                if s_status == 200:
                    state = s_json.get("status_code")
                    if state == "FINISHED":
                        break
                    elif state == "ERROR":
                        err_d = s_json.get("status") or "Meta rejected video processing."
                        return {"success": False, "error": f"Instagram Video Processing Error: {err_d}"}
                    elif state == "EXPIRED":
                        return {"success": False, "error": "Instagram video container expired."}

        # Case 2: Carousel Post (2 to 10 items)
        elif media_items and len(media_items) > 1:
            child_ids = []
            for idx, item in enumerate(media_items[:10]):
                item_url = item.get("url") or ""
                if item_url.startswith("http://"):
                    item_url = "https://" + item_url[len("http://"):]

                if not item_url:
                    continue

                child_container_url = f"https://graph.facebook.com/{graph_version}/{ig_account_id}/media"
                c_data = {
                    "is_carousel_item": "true",
                    "access_token": access_token
                }
                if proof:
                    c_data["appsecret_proof"] = proof

                if item.get("is_video"):
                    c_data["media_type"] = "VIDEO"
                    c_data["video_url"] = item_url
                else:
                    c_data["image_url"] = item_url

                c_status, c_json = _meta_http_post(child_container_url, data=c_data, timeout=30)
                if c_status != 200 or "id" not in c_json:
                    err_msg = _format_meta_error(c_json, f"Failed to create carousel item {idx+1}")
                    return {"success": False, "error": f"Instagram Carousel Item Error: {err_msg}"}

                child_id = c_json["id"]

                if item.get("is_video"):
                    poll_url = f"https://graph.facebook.com/{graph_version}/{child_id}?fields=status_code,status&access_token={access_token}"
                    if proof:
                        poll_url += f"&appsecret_proof={proof}"
                    for _ in range(15):
                        time.sleep(3)
                        s_status, s_json = _meta_http_get(poll_url, timeout=10)
                        if s_status == 200 and s_json.get("status_code") == "FINISHED":
                            break

                child_ids.append(child_id)

            if len(child_ids) < 2:
                return {"success": False, "error": "Instagram carousel requires at least 2 valid media items."}

            car_url = f"https://graph.facebook.com/{graph_version}/{ig_account_id}/media"
            car_data = {
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
                "caption": caption,
                "access_token": access_token
            }
            if proof:
                car_data["appsecret_proof"] = proof

            car_status, car_json = _meta_http_post(car_url, data=car_data, timeout=30)
            if car_status != 200 or "id" not in car_json:
                err_msg = _format_meta_error(car_json, "Failed to create Instagram carousel container")
                return {"success": False, "error": f"Instagram Carousel Error: {err_msg}"}

            creation_id = car_json["id"]

            c_poll_url = f"https://graph.facebook.com/{graph_version}/{creation_id}?fields=status_code,status&access_token={access_token}"
            if proof:
                c_poll_url += f"&appsecret_proof={proof}"
            for _ in range(12):
                time.sleep(2)
                s_status, s_json = _meta_http_get(c_poll_url, timeout=10)
                if s_status == 200 and s_json.get("status_code") == "FINISHED":
                    break

        # Case 3: Single Image Post
        else:
            if not image_url or not (image_url.startswith("http://") or image_url.startswith("https://")):
                return {"success": False, "error": "Instagram requires a publicly accessible HTTPS image or video URL to publish."}

            if image_url.startswith("http://"):
                image_url = "https://" + image_url[len("http://"):]

            container_url = f"https://graph.facebook.com/{graph_version}/{ig_account_id}/media"
            c_data = {
                "image_url": image_url,
                "caption": caption,
                "access_token": access_token
            }
            if proof:
                c_data["appsecret_proof"] = proof

            c_status, c_json = _meta_http_post(container_url, data=c_data, timeout=30)
            if c_status != 200 or "id" not in c_json:
                err_msg = _format_meta_error(c_json, "Failed to create Instagram media container")
                return {"success": False, "error": f"Instagram Container Error: {err_msg}"}

            creation_id = c_json["id"]

            status_url = f"https://graph.facebook.com/{graph_version}/{creation_id}?fields=status_code,status&access_token={access_token}"
            if proof:
                status_url += f"&appsecret_proof={proof}"

            for _ in range(10):
                time.sleep(2)
                s_status, s_json = _meta_http_get(status_url, timeout=10)
                if s_status == 200:
                    container_state = s_json.get("status_code")
                    if container_state == "FINISHED":
                        break
                    elif container_state == "ERROR":
                        err_details = s_json.get("status") or "Meta rejected image processing."
                        return {"success": False, "error": f"Instagram Image Processing Error: {err_details}"}
                    elif container_state == "EXPIRED":
                        return {"success": False, "error": "Instagram media container expired."}
                else:
                    break

        if not creation_id:
            return {"success": False, "error": "Could not create Instagram media container."}

        # Common Publish Step for All Instagram Media Types
        pub_url = f"https://graph.facebook.com/{graph_version}/{ig_account_id}/media_publish"
        p_data = {
            "creation_id": creation_id,
            "access_token": access_token
        }
        if proof:
            p_data["appsecret_proof"] = proof
        p_status, p_json = _meta_http_post(pub_url, data=p_data, timeout=30)

        if p_status == 200 and "id" in p_json:
            media_id = p_json["id"]

            ig_url = None
            try:
                perm_url = f"https://graph.facebook.com/{graph_version}/{media_id}?fields=permalink&access_token={access_token}"
                if proof:
                    perm_url += f"&appsecret_proof={proof}"
                _, perm_res = _meta_http_get(perm_url, timeout=10)
                if isinstance(perm_res, dict) and perm_res.get("permalink"):
                    ig_url = perm_res["permalink"]
            except Exception:
                pass

            if not ig_url:
                username = settings.get("instagram_username", "")
                ig_url = f"https://www.instagram.com/{username}".rstrip("/") if username else "https://www.instagram.com"

            return {
                "success": True,
                "media_id": str(media_id),
                "url": ig_url
            }
        else:
            err_msg = _format_meta_error(p_json, "Instagram media publication failed")
            return {"success": False, "error": f"Instagram Publish Error: {err_msg}"}

    except Exception as e:
        return {"success": False, "error": f"Instagram API Connection Error: {str(e)}"}
@app.route("/social-media", methods=["GET"])
@no_cache
def social_media_hub():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")

    settings = get_social_settings()

    # Check if pre-filling from an existing blog
    blog_id = request.args.get("blog_id")
    prefill = None
    if blog_id:
        try:
            blog = client["globsoft_db"]["blogs"].find_one({"_id": ObjectId(blog_id)})
            if blog:
                blog_slug = blog.get("slug", "")
                site_base = get_site_base_url()
                admin_base = get_admin_base_url()
                blog_url = f"{site_base}/blog/{blog_slug}"
                tags_str = ""
                if blog.get("tags"):
                    raw_tags = [t.strip().replace(" ", "").replace("-", "") for t in blog.get("tags", "").split(",") if t.strip()]
                    tags_str = " ".join([f"#{t}" for t in raw_tags if not t.startswith("#")])

                # Caption strictly with title, Read more... and tags
                caption = f"{blog.get('title')}\n\nRead more... {blog_url}\n\n{tags_str}".strip()

                image_url = ""
                if blog.get("image_data"):
                    image_url = f"{admin_base}/api/blog-images/{blog['_id']}"
                elif blog.get("image_url"):
                    image_url = blog.get("image_url")

                prefill = {
                    "blog_id": str(blog["_id"]),
                    "title": blog.get("title"),
                    "caption": caption.strip(),
                    "link_url": blog_url,
                    "image_url": image_url,
                    "is_blog_share": True,
                }
        except Exception:
            pass

    # Retrieve recent social posts history
    posts_col = client["globsoft_db"]["social_posts"]
    recent_posts = list(posts_col.find().sort("created_at", -1).limit(50))

    total_posts = posts_col.count_documents({})
    fb_count = posts_col.count_documents({"channels": "facebook", "facebook.success": True})
    ig_count = posts_col.count_documents({"channels": "instagram", "instagram.success": True})

    stats = {
        "total": total_posts,
        "facebook": fb_count,
        "instagram": ig_count,
    }

    return render_template(
        "social_media.html",
        settings=settings,
        posts=recent_posts,
        stats=stats,
        prefill=prefill,
        is_admin=is_admin(),
    )


def _process_and_publish_single_post(caption, channels, link_url="", media_files=None, image_url="", video_url="", media_urls_raw="", settings=None, fb_use_meta=True):
    """Internal helper to publish a single post to Facebook and/or Instagram."""
    if not settings:
        settings = get_social_settings()

    media_files = media_files or []
    admin_base = get_admin_base_url()
    all_media_items = []
    video_bytes = None
    video_mime = None
    first_image_bytes = None
    first_image_mime = None

    # 1. Process uploaded files
    for f in media_files:
        if not f or not f.filename:
            continue
        f_bytes = f.read()
        if not f_bytes:
            continue
        is_vid = is_video_mime(f.mimetype, f.filename)
        max_bytes = BLOG_VIDEO_MAX_BYTES if is_vid else BLOG_IMAGE_MAX_BYTES
        if len(f_bytes) > max_bytes:
            return {"success": False, "error": f"File '{f.filename}' exceeds limit of {max_bytes // (1024*1024)} MB."}

        mime = normalize_media_mime(f.mimetype, f.filename) or ("video/mp4" if is_vid else "image/jpeg")
        m_doc = {
            "filename": f.filename,
            "data": f_bytes,
            "mimetype": mime,
            "size": len(f_bytes),
            "created_at": datetime.utcnow(),
            "uploaded_by": session.get("user"),
            "source": "social_media",
        }
        res = client["globsoft_db"]["blog_media"].insert_one(m_doc)
        media_id = str(res.inserted_id)
        pub_url = f"{admin_base}/api/blog-media/{media_id}"

        item = {
            "bytes": f_bytes,
            "mime": mime,
            "url": pub_url,
            "is_video": is_vid,
            "filename": f.filename,
        }
        all_media_items.append(item)
        if is_vid and not video_bytes:
            video_bytes = f_bytes
            video_mime = mime
        if not is_vid and not first_image_bytes:
            first_image_bytes = f_bytes
            first_image_mime = mime

    # 2. Process external/manual URLs
    if video_url:
        all_media_items.append({
            "url": video_url.strip(),
            "is_video": True,
            "bytes": None,
            "mime": "video/mp4"
        })

    if image_url:
        all_media_items.append({
            "url": image_url.strip(),
            "is_video": False,
            "bytes": None,
            "mime": "image/jpeg"
        })

    if media_urls_raw:
        for u in re.split(r'[\r\n,]+', media_urls_raw):
            u_clean = u.strip()
            if u_clean:
                is_u_vid = any(u_clean.lower().endswith(ext) for ext in [".mp4", ".mov", ".m4v", ".webm"])
                all_media_items.append({
                    "url": u_clean,
                    "is_video": is_u_vid,
                    "bytes": None,
                    "mime": "video/mp4" if is_u_vid else "image/jpeg"
                })

    # 3. Determine media classification
    is_video_post = any(m["is_video"] for m in all_media_items)
    is_multi_post = len(all_media_items) > 1

    if is_video_post:
        primary_media_type = "video"
    elif is_multi_post:
        primary_media_type = "carousel"
    elif len(all_media_items) == 1:
        primary_media_type = "image"
    else:
        primary_media_type = "text"

    # Instagram validation
    if "instagram" in channels and primary_media_type == "text":
        return {"success": False, "error": "Instagram strictly requires an image or video to publish. Please attach media or uncheck Instagram."}

    # Meta Credentials Check
    if not settings["is_configured"]:
        return {"success": False, "error": "Meta API credentials are not configured. Please check Social Settings."}

    # 4. Execute Publishing to Selected Channels
    fb_result = None
    ig_result = None
    success_channels = []
    error_channels = []

    primary_video_url = next((m["url"] for m in all_media_items if m.get("is_video")), None)
    primary_image_url = next((m["url"] for m in all_media_items if not m.get("is_video")), None)
    if not primary_image_url and all_media_items:
        primary_image_url = all_media_items[0].get("url")

    # Facebook
    if "facebook" in channels:
        if primary_media_type == "video":
            fb_res = post_to_facebook_page(
                caption=caption,
                video_bytes=video_bytes,
                video_mime=video_mime,
                video_url=primary_video_url,
                link_url=link_url,
                settings=settings
            )
        elif primary_media_type == "carousel":
            fb_res = post_to_facebook_page(
                caption=caption,
                media_items=all_media_items,
                link_url=link_url,
                settings=settings
            )
        else:
            # If sharing a blog with link and fb_use_meta is active, do not attach photo for FB so preview is directly generated from meta tags
            use_fb_meta = bool(fb_use_meta and link_url)
            fb_img_bytes = None if use_fb_meta else first_image_bytes
            fb_img_mime = None if use_fb_meta else first_image_mime
            fb_img_url = None if use_fb_meta else primary_image_url
            fb_res = post_to_facebook_page(
                caption=caption,
                image_bytes=fb_img_bytes,
                image_mime=fb_img_mime,
                image_url=fb_img_url,
                link_url=link_url,
                settings=settings
            )
        fb_result = fb_res
        if fb_res.get("success"):
            success_channels.append("Facebook")
        else:
            error_channels.append(f"Facebook ({fb_res.get('error')})")

    # Instagram
    if "instagram" in channels:
        if primary_media_type == "video":
            ig_res = post_to_instagram_business(
                caption=caption,
                video_url=primary_video_url,
                settings=settings
            )
        elif primary_media_type == "carousel":
            ig_res = post_to_instagram_business(
                caption=caption,
                media_items=all_media_items,
                settings=settings
            )
        else:
            ig_res = post_to_instagram_business(
                caption=caption,
                image_url=primary_image_url,
                settings=settings
            )
        ig_result = ig_res
        if ig_res.get("success"):
            success_channels.append("Instagram")
        else:
            error_channels.append(f"Instagram ({ig_res.get('error')})")

    overall_status = "published" if success_channels and not error_channels else ("partial" if success_channels else "failed")

    post_record = {
        "caption": caption,
        "channels": channels,
        "media_type": primary_media_type,
        "image_url": primary_image_url or (primary_video_url if primary_media_type == "video" else None),
        "video_url": primary_video_url,
        "media_urls": [m["url"] for m in all_media_items if m.get("url")],
        "link_url": link_url,
        "facebook": fb_result,
        "instagram": ig_result,
        "status": overall_status,
        "created_at": datetime.utcnow(),
        "created_by": session.get("user"),
    }
    client["globsoft_db"]["social_posts"].insert_one(post_record)

    return {
        "success": overall_status in ["published", "partial"],
        "status": overall_status,
        "success_channels": success_channels,
        "error_channels": error_channels,
        "record": {
            "media_type": primary_media_type,
            "status": overall_status,
            "facebook": fb_result,
            "instagram": ig_result,
        }
    }


@app.route("/social-media/publish", methods=["POST"])
@no_cache
def publish_social_post():
    if not is_admin():
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return redirect("/")

    caption = request.form.get("caption", "").strip()
    channels = request.form.getlist("channels")
    link_url = request.form.get("link_url", "").strip()
    image_url = request.form.get("image_url", "").strip()
    video_url = request.form.get("video_url", "").strip()
    media_urls = request.form.get("media_urls", "").strip()
    fb_use_meta = request.form.get("fb_use_meta", "1") == "1"

    if not channels:
        flash("Please select at least one channel (Facebook or Instagram) to publish to.", "error")
        return redirect("/social-media")

    if not caption:
        flash("Post caption cannot be empty.", "error")
        return redirect("/social-media")

    # Collect files from media_files or image_file / video_file
    files = request.files.getlist("media_files")
    if not files or all(not f.filename for f in files):
        single_img = request.files.get("image_file")
        single_vid = request.files.get("video_file")
        files = [f for f in [single_img, single_vid] if f and f.filename]

    res = _process_and_publish_single_post(
        caption=caption,
        channels=channels,
        link_url=link_url,
        media_files=files,
        image_url=image_url,
        video_url=video_url,
        media_urls_raw=media_urls,
        fb_use_meta=fb_use_meta
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify(res)

    if res["status"] == "published":
        flash(f"🎉 Successfully published to {' & '.join(res['success_channels'])} at once!", "success")
    elif res["status"] == "partial":
        flash(f"Published to {', '.join(res['success_channels'])}, but encountered errors: {'; '.join(res['error_channels'])}", "error")
    else:
        errs = res.get("error_channels") or [res.get("error", "Unknown publishing error")]
        flash(f"Could not publish post: {'; '.join(errs)}", "error")

    return redirect("/social-media")


@app.route("/social-media/publish-batch", methods=["POST"])
@no_cache
def publish_social_batch():
    """Publish multiple posts in a single request (Batch / Multi-post feature)."""
    if not is_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    posts = data.get("posts", [])

    if not posts or not isinstance(posts, list):
        return jsonify({"success": False, "error": "No posts provided in batch."}), 400

    results = []
    settings = get_social_settings()

    for idx, p in enumerate(posts):
        caption = str(p.get("caption", "")).strip()
        channels = p.get("channels", ["facebook", "instagram"])
        link_url = str(p.get("link_url", "")).strip()
        image_url = str(p.get("image_url", "")).strip()
        video_url = str(p.get("video_url", "")).strip()
        media_urls = str(p.get("media_urls", "")).strip()

        if not caption:
            results.append({"index": idx + 1, "success": False, "error": "Caption cannot be empty"})
            continue

        res = _process_and_publish_single_post(
            caption=caption,
            channels=channels,
            link_url=link_url,
            media_files=None,
            image_url=image_url,
            video_url=video_url,
            media_urls_raw=media_urls,
            settings=settings
        )
        res["index"] = idx + 1
        results.append(res)

    success_count = sum(1 for r in results if r.get("success"))
    return jsonify({
        "success": success_count > 0,
        "total": len(posts),
        "success_count": success_count,
        "results": results
    })


@app.route("/social-media/settings", methods=["POST"])
@no_cache
def update_social_settings():
    if not is_admin():
        return redirect("/")

    fb_page_id = request.form.get("facebook_page_id", "").strip()
    fb_token = request.form.get("facebook_access_token", "").strip()
    ig_account_id = request.form.get("instagram_account_id", "").strip()
    app_id = request.form.get("meta_app_id", "").strip()
    app_secret = request.form.get("meta_app_secret", "").strip()
    default_hashtags = request.form.get("default_hashtags", "").strip()

    client["globsoft_db"]["settings"].update_one(
        {"key": "social_media"},
        {
            "$set": {
                "key": "social_media",
                "facebook_page_id": fb_page_id,
                "facebook_access_token": fb_token,
                "instagram_account_id": ig_account_id,
                "meta_app_id": app_id,
                "meta_app_secret": app_secret,
                "default_hashtags": default_hashtags,
                "updated_at": datetime.utcnow(),
                "updated_by": session.get("user"),
            }
        },
        upsert=True
    )

    flash("Meta API credentials and default settings saved successfully.", "success")
    return redirect("/social-media")


@app.route("/social-media/test-connection", methods=["POST"])
@no_cache
def test_social_connection():
    if not is_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or request.form
    fb_page_id = (data.get("facebook_page_id") or "").strip()
    fb_token = (data.get("facebook_access_token") or "").strip()
    ig_account_id = (data.get("instagram_account_id") or "").strip()
    app_id = (data.get("meta_app_id") or "").strip()
    app_secret = (data.get("meta_app_secret") or "").strip()

    # Fallback to saved / environment settings if fields left blank
    saved = get_social_settings()
    fb_page_id = fb_page_id or saved.get("facebook_page_id")
    fb_token = fb_token or saved.get("facebook_access_token")
    ig_account_id = ig_account_id or saved.get("instagram_account_id")
    app_id = app_id or saved.get("meta_app_id")
    app_secret = app_secret or saved.get("meta_app_secret")

    if not fb_token:
        return jsonify({"success": False, "error": "No Facebook Page Access Token found. Please enter or configure META_PAGE_ACCESS_TOKEN."}), 400

    proof = _get_meta_appsecret_proof(fb_token, app_secret)

    try:
        # 1. Query Facebook Page details
        target = fb_page_id if fb_page_id else "me"
        url = f"https://graph.facebook.com/v19.0/{target}?fields=id,name,category,followers_count,link,instagram_business_account&access_token={fb_token}"
        if proof:
            url += f"&appsecret_proof={proof}"
        status_code, res = _meta_http_get(url, timeout=15)

        if status_code != 200 or "error" in res:
            err = res.get("error", {})
            return jsonify({
                "success": False,
                "error": f"Meta Error ({err.get('code', 'Auth')}): {err.get('message', 'Invalid access token or Page ID')}"
            }), 400

        page_name = res.get("name", "Facebook Page")
        resolved_page_id = res.get("id")
        followers = res.get("followers_count", "N/A")
        connected_ig_id = res.get("instagram_business_account", {}).get("id")

        # 2. Query Instagram Account details
        ig_info = None
        ig_target = ig_account_id or connected_ig_id
        if ig_target:
            ig_url = f"https://graph.facebook.com/v19.0/{ig_target}?fields=id,username,followers_count,name&access_token={fb_token}"
            if proof:
                ig_url += f"&appsecret_proof={proof}"
            ig_status, ig_res = _meta_http_get(ig_url, timeout=15)
            if ig_status == 200 and "username" in ig_res:
                ig_info = {
                    "id": ig_res.get("id"),
                    "username": ig_res.get("username"),
                    "followers": ig_res.get("followers_count", "N/A"),
                    "name": ig_res.get("name", ""),
                }

        # 3. Optional Token Scope Inspection if app_id and app_secret provided
        debug_info = None
        if app_id and app_secret:
            try:
                dbg_url = f"https://graph.facebook.com/v19.0/debug_token?input_token={fb_token}&access_token={app_id}|{app_secret}"
                d_status, d_res = _meta_http_get(dbg_url, timeout=10)
                if d_status == 200 and "data" in d_res:
                    d_data = d_res["data"]
                    debug_info = {
                        "is_valid": d_data.get("is_valid", True),
                        "scopes": d_data.get("scopes", []),
                        "type": d_data.get("type", "PAGE"),
                    }
            except Exception:
                pass

        # Cache page name and Instagram username for UI mockups
        try:
            update_fields = {
                "facebook_page_name": page_name,
            }
            if ig_info and ig_info.get("username"):
                update_fields["instagram_username"] = ig_info["username"]
            if connected_ig_id and not ig_account_id:
                update_fields["instagram_account_id"] = connected_ig_id

            client["globsoft_db"]["settings"].update_one(
                {"key": "social_media"},
                {"$set": update_fields},
                upsert=True
            )
        except Exception:
            pass

        msg = f"✓ Verified! Connected to Facebook Page: {page_name}"
        if ig_info:
            msg += f" & Instagram Account: @{ig_info['username']}"
        elif connected_ig_id:
            msg += f" & Linked Instagram ID: {connected_ig_id}"

        return jsonify({
            "success": True,
            "page_name": page_name,
            "page_id": resolved_page_id,
            "followers": followers,
            "instagram": ig_info,
            "debug": debug_info,
            "appsecret_proof_active": bool(proof),
            "message": msg,
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"Connection check failed: {str(e)}"}), 500


@app.route("/social-media/posts/<post_id>/delete", methods=["POST"])
@no_cache
def delete_social_post(post_id):
    if not is_admin():
        return redirect("/")
    try:
        client["globsoft_db"]["social_posts"].delete_one({"_id": ObjectId(post_id)})
        flash("Social media log entry deleted.", "success")
    except Exception:
        flash("Could not delete social post record.", "error")
    return redirect("/social-media")

@app.route("/api/sitemap.xml", methods=["GET"])
def generate_sitemap():
    """Dynamically generate XML sitemap with homepage and all published blogs."""
    site_base = get_site_base_url()
    now_iso = datetime.utcnow().strftime("%Y-%m-%d")

    urls = [
        f"""  <url>
    <loc>{site_base}/</loc>
    <lastmod>{now_iso}</lastmod>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>""",
        f"""  <url>
    <loc>{site_base}/blog</loc>
    <lastmod>{now_iso}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>"""
    ]

    blogs = client["globsoft_db"]["blogs"].find({"status": "published"}).sort("updated_at", -1)
    for b in blogs:
        slug = b.get("slug")
        if not slug:
            continue
        mod_date = (b.get("updated_at") or b.get("published_at") or datetime.utcnow()).strftime("%Y-%m-%d")
        urls.append(f"""  <url>
    <loc>{site_base}/blog/{slug}</loc>
    <lastmod>{mod_date}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>""")

    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(urls)}
</urlset>"""
    return Response(xml_content, mimetype="application/xml")


# ==================== NOTIFICATION CENTER ====================

def get_admin_notifications(limit=60):
    notifications = []
    try:
        cust_db = client["customer_db"]
        glob_db = client["globsoft_db"]

        # 1. Orders
        orders = list(cust_db["customers"].find({
            "$or": [{"type": "order"}, {"source": "Order Form"}]
        }).sort("date_added", -1).limit(limit))

        for o in orders:
            pkg = o.get("package") or o.get("service") or "Website Order"
            price = o.get("price")
            price_str = f" (रु {price})" if price else ""
            notifications.append({
                "id": str(o.get("_id")),
                "type": "order",
                "badge": "Order",
                "icon": "fa-solid fa-cart-shopping",
                "color": "#79fff7",
                "title": f"New Order: {pkg}{price_str}",
                "description": f"{o.get('name', 'Client')} ({o.get('email', '')}) ordered {pkg}. Company: {o.get('company', 'N/A')}",
                "timestamp": o.get("date_added") or datetime.utcnow(),
                "link": "/customer",
            })

        # 2. Contact Messages
        messages = list(cust_db["customers"].find({
            "$or": [{"type": "inquiry"}, {"source": "Contact Form"}]
        }).sort("date_added", -1).limit(limit))

        for m in messages:
            msg_snippet = (m.get("remark") or m.get("message") or "")[:120]
            notifications.append({
                "id": str(m.get("_id")),
                "type": "message",
                "badge": "Message",
                "icon": "fa-solid fa-envelope",
                "color": "#bca0ff",
                "title": f"New Message from {m.get('name', 'Visitor')}",
                "description": f"{m.get('email', '')}: \"{msg_snippet}...\"",
                "timestamp": m.get("date_added") or datetime.utcnow(),
                "link": "/customer",
            })

        # 3. Blogs Posted
        blogs = list(glob_db["blogs"].find({
            "status": "published"
        }).sort("published_at", -1).limit(limit))

        for b in blogs:
            pub_date = b.get("published_at") or b.get("updated_at") or b.get("created_at")
            author_name = b.get("author") or "Globsoft Writer"
            notifications.append({
                "id": str(b.get("_id")),
                "type": "blog",
                "badge": "Blog Posted",
                "icon": "fa-solid fa-newspaper",
                "color": "#7adb78",
                "title": f"Blog Posted: {b.get('title')}",
                "description": f"Published by {author_name} · Category: {b.get('category', 'Technology')}",
                "timestamp": pub_date or datetime.utcnow(),
                "link": f"/blogs/{b.get('_id')}/edit",
            })

        def get_ts(item):
            ts = item.get("timestamp")
            if isinstance(ts, str):
                try:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    return datetime.min
            elif isinstance(ts, datetime):
                return ts
            return datetime.min

        notifications.sort(key=get_ts, reverse=True)
    except Exception as e:
        app.logger.exception("Error gathering notifications: %s", e)

    return notifications[:limit]


@app.context_processor
def inject_notifications_meta():
    if not session.get("user") or not is_admin():
        return {"notification_count": 0}
    try:
        recent = get_admin_notifications(20)
        return {"notification_count": len(recent)}
    except Exception:
        return {"notification_count": 0}


@app.route("/notifications", methods=["GET"])
@no_cache
def admin_notifications_page():
    if not is_admin():
        return redirect("/blogs" if session.get("role") == "author" else "/")

    all_notifications = get_admin_notifications(100)
    filter_type = request.args.get("filter", "all")
    if filter_type in ["order", "message", "blog"]:
        filtered = [n for n in all_notifications if n["type"] == filter_type]
    else:
        filtered = all_notifications

    counts = {
        "all": len(all_notifications),
        "order": sum(1 for n in all_notifications if n["type"] == "order"),
        "message": sum(1 for n in all_notifications if n["type"] == "message"),
        "blog": sum(1 for n in all_notifications if n["type"] == "blog"),
    }

    return render_template(
        "notifications.html",
        notifications=filtered,
        counts=counts,
        current_filter=filter_type,
    )


@app.route("/api/admin/notifications", methods=["GET"])
@no_cache
def api_admin_notifications():
    if not is_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    notifications = get_admin_notifications(30)
    serialized = []
    for n in notifications:
        item = dict(n)
        if isinstance(item.get("timestamp"), datetime):
            item["timestamp"] = item["timestamp"].isoformat()
        serialized.append(item)
    return jsonify({"success": True, "notifications": serialized})


if __name__ == "__main__":
    app.run(debug=True, port=5000)

