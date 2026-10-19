from flask import Flask, flash, render_template, request, session, redirect, jsonify, make_response, Response
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

client = MongoClient(mongo_uri)

app = Flask(__name__)
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
        "image_url": f"/api/blog-images/{document['_id']}" if document.get("image_data") else None,
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
}


def get_site_content():
    content = client["globsoft_db"]["site_content"].find_one({"_id": "homepage"})
    return {
        "pricing": content.get("pricing", DEFAULT_SITE_CONTENT["pricing"]) if content else DEFAULT_SITE_CONTENT["pricing"],
        "clients": content.get("clients", DEFAULT_SITE_CONTENT["clients"]) if content else DEFAULT_SITE_CONTENT["clients"],
    }


@app.route("/api/site-content", methods=["GET"])
def site_content():
    response = jsonify(get_site_content())
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
            "pricing": current["pricing"],
            "clients": current["clients"],
            "updated_at": datetime.now(),
            "updated_by": session["user"],
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


@app.route("/api/blog-images/<blog_id>", methods=["GET"])
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
    resp = Response(document["image_data"], mimetype=document.get("image_mime", "image/jpeg"))
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route("/api/blog-media/<media_id>", methods=["GET"])
def blog_media(media_id):
    try:
        doc = client["globsoft_db"]["blog_media"].find_one({"_id": ObjectId(media_id)})
    except Exception:
        return jsonify({"success": False, "error": "Invalid image ID"}), 400
    if not doc or not doc.get("data"):
        return jsonify({"success": False, "error": "Image not found"}), 404
    resp = Response(doc["data"], mimetype=doc.get("mimetype", "image/jpeg"))
    resp.headers['Cache-Control'] = 'public, max-age=31536000'
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
    if not image or not image.filename:
        if request.form.get("remove_cover_image") == "1":
            document["image_data"] = None
            document["image_mime"] = None
        return
    image_data = image.read()
    if len(image_data) > BLOG_IMAGE_MAX_BYTES:
        raise ValueError(f"Cover image must be {BLOG_IMAGE_MAX_BYTES // (1024 * 1024)} MB or smaller.")
    mime = normalize_image_mime(image.mimetype, image.filename)
    if not mime:
        raise ValueError("Use a JPG, PNG, WEBP, GIF, or SVG cover image.")
    document["image_data"] = image_data
    document["image_mime"] = mime


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
    return render_template("authors.html", authors=authors, stats=stats)
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
        if not new_name or len(new_name) > 60:
            flash("Display name must be between 1 and 60 characters.", "error")
        else:
            old_name = author.get("name")
            if author.get("_id"):
                authors_col.update_one(
                    {"_id": author["_id"]},
                    {"$set": {"name": new_name, "bio": bio, "updated_at": datetime.utcnow()}}
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
            flash("Profile updated! Your new display name is now active on all your articles.", "success")
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
                    "active": False,  # Pending approval by admin on first creation
                    "role": "author",
                    "created_at": now,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

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
    Reverse proxy for Clerk Frontend API on adminglobsoft.vercel.app/__clerk.
    Enables Clerk on *.vercel.app production domain without custom DNS records.
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
    
    # Exclude headers that cause upstream encoding conflicts
    fwd_headers = {k: v for k, v in request.headers if k.lower() not in {"host", "content-length", "accept-encoding"}}
    fwd_headers["Host"] = "frontend-api.clerk.dev"
    proxy_url = os.getenv("CLERK_PROXY_URL", "https://adminglobsoft.vercel.app/__clerk")
    fwd_headers["Clerk-Proxy-Url"] = proxy_url
    if clerk_secret_key:
        fwd_headers["Clerk-Secret-Key"] = clerk_secret_key
    fwd_headers["X-Forwarded-Host"] = request.host
    fwd_headers["X-Forwarded-Proto"] = request.scheme
    if request.remote_addr:
        fwd_headers["X-Forwarded-For"] = request.remote_addr

    if requests is not None:
        try:
            upstream_resp = requests.request(
                method=request.method,
                url=target_url,
                headers=fwd_headers,
                data=request.get_data() if request.method not in {"GET", "HEAD"} else None,
                allow_redirects=False,
                timeout=15,
            )
            excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
            resp_headers = [(k, v) for k, v in upstream_resp.headers.items() if k.lower() not in excluded]
            return Response(upstream_resp.content, status=upstream_resp.status_code, headers=resp_headers)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Fallback to urllib with gzip decompression if requests is absent
    import gzip
    req = Request(
        target_url,
        data=request.get_data() if request.method not in {"GET", "HEAD", "OPTIONS"} else None,
        headers=fwd_headers,
        method=request.method,
    )
    try:
        with urlopen(req, timeout=15) as resp:
            data = resp.read()
            if resp.getheader("Content-Encoding") == "gzip":
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
            excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
            resp_headers = [(name, value) for name, value in resp.getheaders() if name.lower() not in excluded_headers]
            return Response(data, status=resp.getcode(), headers=resp_headers)
    except Exception as e:
        if hasattr(e, "read"):
            try:
                err_data = e.read()
                if getattr(e, "headers", None) and e.headers.get("Content-Encoding") == "gzip":
                    try:
                        err_data = gzip.decompress(err_data)
                    except Exception:
                        pass
                return Response(err_data, status=getattr(e, "code", 500), content_type="application/json")
            except Exception:
                pass
        return jsonify({"error": str(e)}), 500




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
        proxy_url = os.getenv("CLERK_PROXY_URL", "https://adminglobsoft.vercel.app/__clerk")

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
    customers = list(collection.find())
    
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

def get_social_settings():
    """Fetch Facebook and Instagram credentials from MongoDB, fallback to .env."""
    doc = client["globsoft_db"]["settings"].find_one({"key": "social_media"}) or {}
    fb_page_id = doc.get("facebook_page_id") or os.environ.get("FACEBOOK_PAGE_ID", "").strip()
    fb_token = doc.get("facebook_access_token") or os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
    ig_account_id = doc.get("instagram_account_id") or os.environ.get("INSTAGRAM_ACCOUNT_ID", "").strip()
    default_hashtags = doc.get("default_hashtags", "#GlobsoftTech #WebDevelopment #NepalTech #SoftwareCompany #TechSolutions")
    
    return {
        "facebook_page_id": fb_page_id,
        "facebook_access_token": fb_token,
        "instagram_account_id": ig_account_id,
        "default_hashtags": default_hashtags,
        "is_configured": bool(fb_page_id and fb_token),
    }


def _meta_http_post(url, data=None, files=None, timeout=30):
    """POST helper with requests or urllib fallback."""
    if requests is not None:
        resp = requests.post(url, data=data, files=files, timeout=timeout)
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"error": {"message": resp.text}}
    
    if files:
        return 400, {"error": {"message": "Binary image file upload requires the 'requests' package."}}
    
    encoded = urlencode(data or {}).encode("utf-8")
    req = Request(url, data=encoded, headers={"User-Agent": "GlobsoftAdmin/1.0"})
    try:
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
    """GET helper with requests or urllib fallback."""
    if requests is not None:
        resp = requests.get(url, timeout=timeout)
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"error": {"message": resp.text}}
    
    req = Request(url, headers={"User-Agent": "GlobsoftAdmin/1.0"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.getcode(), json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        if hasattr(exc, "read"):
            try:
                return getattr(exc, "code", 400), json.loads(exc.read().decode("utf-8"))
            except Exception:
                pass
        return 500, {"error": {"message": str(exc)}}


def post_to_facebook_page(caption, image_bytes=None, image_mime=None, image_url=None, link_url=None, settings=None):
    """
    Publish a post to Facebook Page via Meta Graph API.
    Supports direct multipart image upload or feed link post.
    """
    if not settings:
        settings = get_social_settings()
    
    page_id = settings.get("facebook_page_id")
    access_token = settings.get("facebook_access_token")
    
    if not page_id or not access_token:
        return {"success": False, "error": "Facebook Page ID or Page Access Token is missing."}
    
    graph_version = "v19.0"
    
    try:
        if image_bytes:
            # Multipart binary photo upload
            url = f"https://graph.facebook.com/{graph_version}/{page_id}/photos"
            files = {
                "source": ("social_post_image.jpg", image_bytes, image_mime or "image/jpeg")
            }
            data = {
                "caption": caption,
                "access_token": access_token
            }
            status_code, res_json = _meta_http_post(url, data=data, files=files, timeout=30)
            if status_code == 200 and ("id" in res_json or "post_id" in res_json):
                post_id = res_json.get("post_id") or res_json.get("id")
                return {
                    "success": True,
                    "post_id": str(post_id),
                    "url": f"https://www.facebook.com/{post_id}"
                }
            else:
                err_msg = res_json.get("error", {}).get("message", str(res_json))
                return {"success": False, "error": f"Facebook Error: {err_msg}"}
        
        elif image_url and (image_url.startswith("http://") or image_url.startswith("https://")):
            # Public image URL photo post
            url = f"https://graph.facebook.com/{graph_version}/{page_id}/photos"
            data = {
                "url": image_url,
                "caption": caption,
                "access_token": access_token
            }
            status_code, res_json = _meta_http_post(url, data=data, timeout=30)
            if status_code == 200 and ("id" in res_json or "post_id" in res_json):
                post_id = res_json.get("post_id") or res_json.get("id")
                return {
                    "success": True,
                    "post_id": str(post_id),
                    "url": f"https://www.facebook.com/{post_id}"
                }
            else:
                err_msg = res_json.get("error", {}).get("message", str(res_json))
                return {"success": False, "error": f"Facebook Error: {err_msg}"}
        
        else:
            # Text or link post to feed
            url = f"https://graph.facebook.com/{graph_version}/{page_id}/feed"
            data = {
                "message": caption,
                "access_token": access_token
            }
            if link_url:
                data["link"] = link_url
            status_code, res_json = _meta_http_post(url, data=data, timeout=30)
            if status_code == 200 and "id" in res_json:
                post_id = res_json["id"]
                return {
                    "success": True,
                    "post_id": str(post_id),
                    "url": f"https://www.facebook.com/{post_id}"
                }
            else:
                err_msg = res_json.get("error", {}).get("message", str(res_json))
                return {"success": False, "error": f"Facebook Error: {err_msg}"}
    except Exception as e:
        return {"success": False, "error": f"Facebook API Connection Error: {str(e)}"}


def post_to_instagram_business(caption, image_url, settings=None):
    """
    Publish a photo post to Instagram Business Account via Meta Graph API.
    Step 1: Create media container POST /{ig_account_id}/media
    Step 2: Publish container POST /{ig_account_id}/media_publish
    """
    if not settings:
        settings = get_social_settings()
    
    ig_account_id = settings.get("instagram_account_id")
    access_token = settings.get("facebook_access_token")
    
    if not ig_account_id or not access_token:
        return {"success": False, "error": "Instagram Business Account ID or Access Token is missing."}
    
    if not image_url or not (image_url.startswith("http://") or image_url.startswith("https://")):
        return {"success": False, "error": "Instagram requires a publicly accessible image URL (JPEG/PNG) to publish."}
    
    graph_version = "v19.0"
    
    try:
        # Step 1: Create media container
        container_url = f"https://graph.facebook.com/{graph_version}/{ig_account_id}/media"
        c_data = {
            "image_url": image_url,
            "caption": caption,
            "access_token": access_token
        }
        c_status, c_json = _meta_http_post(container_url, data=c_data, timeout=30)
        
        if c_status != 200 or "id" not in c_json:
            err_msg = c_json.get("error", {}).get("message", str(c_json))
            return {"success": False, "error": f"Instagram Container Error: {err_msg}"}
        
        creation_id = c_json["id"]
        
        # Pause briefly to allow Meta to download and process the image
        time.sleep(2)
        
        # Step 2: Publish container
        pub_url = f"https://graph.facebook.com/{graph_version}/{ig_account_id}/media_publish"
        p_data = {
            "creation_id": creation_id,
            "access_token": access_token
        }
        p_status, p_json = _meta_http_post(pub_url, data=p_data, timeout=30)
        
        if p_status == 200 and "id" in p_json:
            media_id = p_json["id"]
            return {
                "success": True,
                "media_id": str(media_id),
                "url": "https://www.instagram.com"
            }
        else:
            err_msg = p_json.get("error", {}).get("message", str(p_json))
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
                site_base = os.environ.get("SITE_URL", "https://globsoft.vercel.app").rstrip("/")
                blog_url = f"{site_base}/blog/{blog_slug}"
                tags_str = ""
                if blog.get("tags"):
                    raw_tags = [t.strip().replace(" ", "").replace("-", "") for t in blog.get("tags", "").split(",") if t.strip()]
                    tags_str = " ".join([f"#{t}" for t in raw_tags if not t.startswith("#")])
                
                caption = f"🚀 New on the Globsoft Tech Blog:\n\"{blog.get('title')}\"\n\n{blog.get('excerpt', '')}\n\n👉 Read full article: {blog_url}\n\n{tags_str or settings.get('default_hashtags', '')}"
                
                image_url = ""
                if blog.get("image_data"):
                    image_url = f"{site_base}/api/blog-images/{blog['_id']}"
                
                prefill = {
                    "blog_id": str(blog["_id"]),
                    "title": blog.get("title"),
                    "caption": caption.strip(),
                    "link_url": blog_url,
                    "image_url": image_url,
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
    simulation_mode = request.form.get("simulation_mode") == "1"
    
    if not channels:
        flash("Please select at least one channel (Facebook or Instagram) to publish to.", "error")
        return redirect("/social-media")
    
    if not caption:
        flash("Post caption cannot be empty.", "error")
        return redirect("/social-media")
    
    settings = get_social_settings()
    
    # Handle image upload if a file was attached
    image_file = request.files.get("image_file")
    image_bytes = None
    image_mime = None
    saved_media_url = image_url
    
    if image_file and image_file.filename:
        image_bytes = image_file.read()
        if len(image_bytes) > BLOG_IMAGE_MAX_BYTES:
            flash(f"Attached image exceeds max size of {BLOG_IMAGE_MAX_BYTES // (1024 * 1024)} MB.", "error")
            return redirect("/social-media")
        
        image_mime = normalize_image_mime(image_file.mimetype, image_file.filename) or "image/jpeg"
        media_doc = {
            "filename": image_file.filename,
            "data": image_bytes,
            "mimetype": image_mime,
            "size": len(image_bytes),
            "created_at": datetime.utcnow(),
            "uploaded_by": session.get("user"),
            "source": "social_media",
        }
        res = client["globsoft_db"]["blog_media"].insert_one(media_doc)
        media_id = str(res.inserted_id)
        
        # Determine public URL
        site_base = os.environ.get("SITE_URL", "https://globsoft.vercel.app").rstrip("/")
        saved_media_url = f"{site_base}/api/blog-media/{media_id}"
        if not image_url:
            image_url = saved_media_url

    # Check if simulation/test mode is selected or if credentials are unconfigured
    if simulation_mode or not settings["is_configured"]:
        sim_timestamp = int(time.time())
        fb_result = {
            "success": True,
            "post_id": f"sim_fb_{sim_timestamp}",
            "simulated": True,
            "url": "https://facebook.com/globsofttech",
        } if "facebook" in channels else None
        
        ig_result = {
            "success": True,
            "media_id": f"sim_ig_{sim_timestamp}",
            "simulated": True,
            "url": "https://instagram.com/globsofttech",
        } if "instagram" in channels else None
        
        post_record = {
            "caption": caption,
            "channels": channels,
            "image_url": saved_media_url,
            "link_url": link_url,
            "facebook": fb_result,
            "instagram": ig_result,
            "status": "simulated",
            "created_at": datetime.utcnow(),
            "created_by": session.get("user"),
        }
        client["globsoft_db"]["social_posts"].insert_one(post_record)
        
        if not settings["is_configured"]:
            flash("Published in Test / Simulation Mode. To post live to your official Facebook Page & Instagram, enter your Meta Page Token in Settings.", "success")
        else:
            flash("Post published in Test / Simulation Mode! Log entry saved.", "success")
        return redirect("/social-media")

    # LIVE PUBLISHING VIA META GRAPH API
    fb_result = None
    ig_result = None
    success_channels = []
    error_channels = []
    
    if "facebook" in channels:
        fb_res = post_to_facebook_page(
            caption=caption,
            image_bytes=image_bytes,
            image_mime=image_mime,
            image_url=image_url,
            link_url=link_url,
            settings=settings
        )
        fb_result = fb_res
        if fb_res.get("success"):
            success_channels.append("Facebook")
        else:
            error_channels.append(f"Facebook ({fb_res.get('error')})")
    
    if "instagram" in channels:
        # Instagram requires a public image URL
        effective_ig_img = image_url
        if effective_ig_img and not (effective_ig_img.startswith("http://") or effective_ig_img.startswith("https://")):
            site_base = os.environ.get("SITE_URL", "https://globsoft.vercel.app").rstrip("/")
            effective_ig_img = f"{site_base}{effective_ig_img}" if effective_ig_img.startswith("/") else f"{site_base}/{effective_ig_img}"
            
        ig_res = post_to_instagram_business(
            caption=caption,
            image_url=effective_ig_img,
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
        "image_url": saved_media_url,
        "link_url": link_url,
        "facebook": fb_result,
        "instagram": ig_result,
        "status": overall_status,
        "created_at": datetime.utcnow(),
        "created_by": session.get("user"),
    }
    client["globsoft_db"]["social_posts"].insert_one(post_record)
    
    if overall_status == "published":
        flash(f"🎉 Successfully published to {' & '.join(success_channels)} at once!", "success")
    elif overall_status == "partial":
        flash(f"Published to {', '.join(success_channels)}, but had issues with: {'; '.join(error_channels)}", "error")
    else:
        flash(f"Could not publish post: {'; '.join(error_channels)}", "error")
    
    return redirect("/social-media")


@app.route("/social-media/settings", methods=["POST"])
@no_cache
def update_social_settings():
    if not is_admin():
        return redirect("/")
    
    fb_page_id = request.form.get("facebook_page_id", "").strip()
    fb_token = request.form.get("facebook_access_token", "").strip()
    ig_account_id = request.form.get("instagram_account_id", "").strip()
    default_hashtags = request.form.get("default_hashtags", "").strip()
    
    client["globsoft_db"]["settings"].update_one(
        {"key": "social_media"},
        {
            "$set": {
                "key": "social_media",
                "facebook_page_id": fb_page_id,
                "facebook_access_token": fb_token,
                "instagram_account_id": ig_account_id,
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
    
    # If not provided in request, fallback to saved settings
    if not fb_token:
        saved = get_social_settings()
        fb_page_id = fb_page_id or saved.get("facebook_page_id")
        fb_token = fb_token or saved.get("facebook_access_token")
        ig_account_id = ig_account_id or saved.get("instagram_account_id")
    
    if not fb_token:
        return jsonify({"success": False, "error": "Please enter a Facebook Page Access Token to test."}), 400
    
    try:
        # 1. Query Facebook Page details
        target = fb_page_id if fb_page_id else "me"
        url = f"https://graph.facebook.com/v19.0/{target}?fields=id,name,category,followers_count,instagram_business_account&access_token={fb_token}"
        status_code, res = _meta_http_get(url, timeout=15)
        
        if status_code != 200 or "error" in res:
            err = res.get("error", {})
            return jsonify({
                "success": False,
                "error": f"Meta Error ({err.get('code', 'Auth')}): {err.get('message', 'Invalid access token or Page ID')}"
            }), 400
        
        page_name = res.get("name", "Facebook Page")
        followers = res.get("followers_count", "N/A")
        connected_ig_id = res.get("instagram_business_account", {}).get("id")
        
        ig_info = None
        ig_target = ig_account_id or connected_ig_id
        if ig_target:
            ig_url = f"https://graph.facebook.com/v19.0/{ig_target}?fields=id,username,followers_count,name&access_token={fb_token}"
            ig_status, ig_res = _meta_http_get(ig_url, timeout=15)
            if ig_status == 200 and "username" in ig_res:
                ig_info = {
                    "id": ig_res.get("id"),
                    "username": ig_res.get("username"),
                    "followers": ig_res.get("followers_count", "N/A"),
                }
        
        msg = f"✓ Verified! Connected to Facebook Page: {page_name}"
        if ig_info:
            msg += f" & Instagram Account: @{ig_info['username']}"
        elif connected_ig_id:
            msg += f" & Linked Instagram ID: {connected_ig_id}"
            
        return jsonify({
            "success": True,
            "page_name": page_name,
            "page_id": res.get("id"),
            "followers": followers,
            "instagram": ig_info,
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)

