import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import time
from http.cookies import SimpleCookie
from email.message import EmailMessage
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from mysql.connector import Error, IntegrityError

from db import (
    add_favorite,
    cancel_order,
    create_listing,
    create_message,
    count_unread_messages,
    create_order,
    create_user,
    find_listings,
    find_messages,
    find_orders_for_user,
    find_order_status_notification,
    find_sales_for_user,
    find_user_thread,
    find_user_messages,
    mark_messages_read,
    find_favorite_ids,
    find_favorite_listings,
    find_user_by_email,
    find_user_by_id,
    initialize_database,
    update_user_password,
    update_listing,
    update_order_status,
    remove_favorite,
)

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
SESSION_SECRET = os.getenv("NCART_SESSION_SECRET", "ncart-local-session-secret")
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_STATES = {}
GOOGLE_REGISTRATIONS = {}
RESET_TOKENS = {}
SESSIONS = {}


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return f"pbkdf2_sha256$240000${salt.hex()}${digest.hex()}"


def verify_password(password, stored_hash):
    try:
        algorithm, iterations, salt_hex, digest_hex = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def make_session_cookie(user_id):
    payload = str(int(user_id))
    signature = hmac.new(
        SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def read_signed_session(value):
    try:
        payload, signature = value.split(".", 1)
        expected = hmac.new(
            SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        user_id = int(payload)
        return {"id": user_id, "email": ""}
    except (AttributeError, TypeError, ValueError):
        return None


class NCartHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/logout":
            self.handle_logout()
            return
        if route == "/api/listings":
            self.handle_listings_get()
            return
        if route == "/api/messages":
            self.handle_messages_get()
            return
        if route == "/api/favorites":
            self.handle_favorites_get()
            return
        if route == "/api/orders":
            self.handle_orders_get()
            return
        if route == "/auth/google":
            self.handle_google_start()
            return
        if route == "/auth/google/callback":
            self.handle_google_callback(parse_qs(urlparse(self.path).query))
            return
        if route == "/":
            self.path = "/templates/index.html"
        elif route == "/login":
            self.path = "/templates/login.html"
        elif route == "/create-account":
            self.path = "/templates/create-account.html"
        elif route == "/reset-password":
            self.path = "/templates/reset-password.html"
        elif route == "/home":
            self.path = "/templates/home.html"
        elif route == "/dashboard":
            self.path = "/templates/home.html"
        elif route in {
            "/marketplace",
            "/favorites",
            "/listings",
            "/messages",
            "/orders",
            "/profile",
        }:
            self.path = f"/templates{route}.html"
        super().do_GET()

    def handle_google_start(self):
        client_id = os.getenv("NCART_GOOGLE_CLIENT_ID", "")
        client_secret = os.getenv("NCART_GOOGLE_CLIENT_SECRET", "")
        if not client_id or not client_secret or client_id.startswith("your_") or client_secret.startswith("your_"):
            self.redirect_with_message(
                "/templates/login.html",
                "Add NCART_GOOGLE_CLIENT_ID and NCART_GOOGLE_CLIENT_SECRET to your local .env file, then restart NCart.",
                "error",
            )
            return

        state = secrets.token_urlsafe(24)
        GOOGLE_STATES[state] = time.time() + 300
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": os.getenv(
                    "NCART_GOOGLE_REDIRECT_URI",
                    "http://127.0.0.1:8000/auth/google/callback",
                ),
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "access_type": "online",
            }
        )
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"{GOOGLE_AUTH_URL}?{query}")
        self.end_headers()

    def handle_google_callback(self, values):
        state = values.get("state", [""])[0]
        expires_at = GOOGLE_STATES.pop(state, 0)
        if not state or expires_at < time.time():
            self.redirect_with_message("/templates/login.html", "Google sign-in expired. Try again.", "error")
            return

        error = values.get("error", [""])[0]
        code = values.get("code", [""])[0]
        if error or not code:
            self.redirect_with_message("/templates/login.html", "Google sign-in was cancelled.", "error")
            return

        try:
            token_data = self.google_request(
                GOOGLE_TOKEN_URL,
                {
                    "code": code,
                    "client_id": os.getenv("NCART_GOOGLE_CLIENT_ID", ""),
                    "client_secret": os.getenv("NCART_GOOGLE_CLIENT_SECRET", ""),
                    "redirect_uri": os.getenv(
                        "NCART_GOOGLE_REDIRECT_URI",
                        "http://127.0.0.1:8000/auth/google/callback",
                    ),
                    "grant_type": "authorization_code",
                },
            )
            profile = self.google_request(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            email = profile.get("email", "").strip().lower()
            full_name = profile.get("name", "Google User").strip()
            if not email or not profile.get("email_verified"):
                raise ValueError("Google did not return a verified email address.")

            user = find_user_by_email(email)
            if user is None:
                registration_token = secrets.token_urlsafe(32)
                GOOGLE_REGISTRATIONS[registration_token] = {
                    "expires_at": time.time() + 600,
                    "email": email,
                    "full_name": full_name,
                    "given_name": profile.get("given_name", "").strip(),
                    "family_name": profile.get("family_name", "").strip(),
                }
                self.redirect_with_message(
                    "/templates/create-account.html",
                    "Google account verified. Complete your NCart registration.",
                    "message",
                    {
                        "google_token": registration_token,
                        "google_email": email,
                        "google_name": full_name,
                        "google_given_name": profile.get("given_name", "").strip(),
                        "google_family_name": profile.get("family_name", "").strip(),
                    },
                )
                return
            token = secrets.token_urlsafe(32)
            SESSIONS[token] = {"id": int(user["id"]), "email": user["email"]}
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/templates/login.html?message=Google+login+successful.+Welcome+back.&login_success=1")
            self.send_header("Set-Cookie", f"ncart_session={make_session_cookie(user['id'])}; Path=/; HttpOnly; SameSite=Lax")
            self.end_headers()
        except (Error, KeyError, ValueError, OSError, json.JSONDecodeError):
            self.redirect_with_message("/templates/login.html", "Google sign-in failed. Try again.", "error")

    @staticmethod
    def google_request(url, data=None, headers=None):
        request = Request(url, headers=headers or {}, method="POST" if data else "GET")
        if data:
            request.data = urlencode(data).encode()
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def do_POST(self):
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        if route == "/api/listings":
            self.handle_listing_create(body)
            return
        if route == "/api/messages":
            self.handle_message_create(body)
            return
        if route == "/api/favorites":
            self.handle_favorite_create(body)
            return
        if route == "/api/orders":
            self.handle_order_create(body)
            return
        values = parse_qs(body, keep_blank_values=True)

        if route == "/login":
            self.handle_login(values)
        elif route == "/register":
            self.handle_register(values)
        elif route == "/reset-password":
            self.handle_reset_password(values)
        elif route == "/request-reset":
            self.handle_reset_request(values)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        route = urlparse(self.path).path
        if route.startswith("/api/orders/"):
            length = int(self.headers.get("Content-Length", "0"))
            self.handle_order_status_update(route.removeprefix("/api/orders/"), self.rfile.read(length).decode())
            return
        if not route.startswith("/api/listings/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.handle_listing_update(route.removeprefix("/api/listings/"), self.rfile.read(length).decode())

    def do_DELETE(self):
        route = urlparse(self.path).path
        if route.startswith("/api/favorites/"):
            self.handle_favorite_delete(route.removeprefix("/api/favorites/"))
            return
        if route.startswith("/api/orders/"):
            self.handle_order_cancel(route.removeprefix("/api/orders/"))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def handle_listings_get(self):
        user = self.current_user()
        mine = parse_qs(urlparse(self.path).query).get("mine", [""])[0] == "1"
        if mine and user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            listings = find_listings(user["id"] if mine else None)
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        for listing in listings:
            listing["id"] = int(listing["id"])
            listing["price"] = str(listing["price"])
            listing["created_at"] = listing["created_at"].isoformat()
        self.send_json({"viewer_id": int(user["id"]) if user else None, "listings": listings})

    def handle_favorites_get(self):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            listings = find_favorite_listings(user["id"])
            favorite_ids = find_favorite_ids(user["id"])
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        for listing in listings:
            listing["id"] = int(listing["id"])
            listing["price"] = str(listing["price"])
            listing["created_at"] = listing["created_at"].isoformat()
        self.send_json({
            "favorite_ids": [int(listing_id) for listing_id in favorite_ids],
            "listings": listings,
        })

    def handle_orders_get(self):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            orders = find_orders_for_user(user["id"])
            sales = find_sales_for_user(user["id"])
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        for order in orders:
            order["id"] = int(order["id"])
            order["listing_id"] = int(order["listing_id"])
            order["seller_id"] = int(order["seller_id"])
            order["total_price"] = str(order["total_price"])
            order["created_at"] = order["created_at"].isoformat()
            order["can_cancel"] = bool(order["can_cancel"])
        for order in sales:
            order["id"] = int(order["id"])
            order["listing_id"] = int(order["listing_id"])
            order["buyer_id"] = int(order["buyer_id"])
            order["seller_id"] = int(order["seller_id"])
            order["total_price"] = str(order["total_price"])
            order["created_at"] = order["created_at"].isoformat()
        self.send_json({"viewer_id": int(user["id"]), "orders": orders, "sales": sales})

    def handle_order_status_update(self, order_id, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            parsed_order_id = int(order_id)
            values = json.loads(body)
            status = str(values.get("status", "")).strip().lower()
            if parsed_order_id <= 0:
                raise ValueError
            updated, error = update_order_status(user["id"], parsed_order_id, status)
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json({"error": "Invalid order status."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not updated:
            self.send_json({"error": error}, HTTPStatus.CONFLICT)
            return
        try:
            notification = find_order_status_notification(parsed_order_id)
            if notification and notification.get("buyer_email"):
                self.send_order_status_email(notification)
        except (Error, OSError, ValueError, smtplib.SMTPException):
            pass
        self.send_json({"message": "Order status updated."})

    def handle_order_cancel(self, order_id):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            parsed_order_id = int(order_id)
            if parsed_order_id <= 0:
                raise ValueError
            cancelled, error = cancel_order(user["id"], parsed_order_id)
        except ValueError:
            self.send_json({"error": "Invalid order."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not cancelled:
            self.send_json({"error": error}, HTTPStatus.CONFLICT)
            return
        self.send_json({"message": "Order cancelled."})

    def handle_order_create(self, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            values = json.loads(body)
            listing_id = int(values.get("listing_id"))
            payment_method = str(values.get("payment_method", "Cash on Delivery") or "Cash on Delivery").strip()
            if listing_id <= 0:
                raise ValueError
            if payment_method not in {"Cash on Delivery", "GCash", "Bank Transfer"}:
                payment_method = "Cash on Delivery"
            order_id, error = create_order(user["id"], listing_id, payment_method)
            if error:
                self.send_json({"error": error}, HTTPStatus.BAD_REQUEST)
                return
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json({"error": "Invalid listing."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"id": int(order_id)}, HTTPStatus.CREATED)

    def handle_messages_get(self):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            query = parse_qs(urlparse(self.path).query)
            if not query.get("listing_id") and not query.get("other_user_id"):
                conversations = find_user_messages(user["id"])
                for item in conversations:
                    item["id"] = int(item["id"])
                    item["listing_id"] = int(item["listing_id"])
                    item["other_user_id"] = int(item["other_user_id"])
                    item["unread_count"] = int(item["unread_count"] or 0)
                    item["created_at"] = item["created_at"].isoformat()
                self.send_json({
                    "viewer_id": int(user["id"]),
                    "unread_count": int(count_unread_messages(user["id"])),
                    "conversations": conversations,
                })
                return
            other_user_id = int(query.get("other_user_id", [""])[0])
            listing_value = query.get("listing_id", [""])[0]
            listing_id = int(listing_value) if listing_value else None
            if other_user_id <= 0 or other_user_id == user["id"]:
                raise ValueError
            messages = find_user_thread(user["id"], other_user_id)
            mark_messages_read(user["id"], None, other_user_id)
        except (ValueError, TypeError):
            self.send_json({"error": "Invalid conversation."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        for message in messages:
            message["id"] = int(message["id"])
            message["sender_id"] = int(message["sender_id"])
            message["recipient_id"] = int(message["recipient_id"])
            message["created_at"] = message["created_at"].isoformat()
        listing = None
        if messages:
            selected_message = next(
                (message for message in messages if message["listing_id"] == listing_id),
                messages[-1],
            )
            listing = {
                "id": int(selected_message["listing_id"]),
                "seller_id": int(selected_message["listing_seller_id"]),
                "title": selected_message["listing_title"],
                "category": selected_message["listing_category"],
                "price": str(selected_message["listing_price"]),
                "image_url": selected_message["listing_image"] or "",
            }
        self.send_json({"viewer_id": int(user["id"]), "listing": listing, "messages": messages})

    def handle_message_create(self, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            values = json.loads(body)
            listing_id = int(values.get("listing_id"))
            recipient_id = int(values.get("recipient_id"))
            text = str(values.get("body", "")).strip()
            reply_to_id = values.get("reply_to_id")
            reply_to_id = int(reply_to_id) if reply_to_id else None
            if listing_id <= 0 or recipient_id <= 0 or recipient_id == user["id"] or not text or len(text) > 2000:
                raise ValueError
            message_id = create_message(listing_id, user["id"], recipient_id, text, reply_to_id)
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            self.send_json({"error": "Enter a valid message."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"id": int(message_id)}, HTTPStatus.CREATED)

    def handle_favorite_create(self, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            listing_id = int(json.loads(body).get("listing_id"))
            if listing_id <= 0:
                raise ValueError
            add_favorite(user["id"], listing_id)
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            self.send_json({"error": "Invalid listing."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"listing_id": listing_id}, HTTPStatus.CREATED)

    def handle_favorite_delete(self, listing_id):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            listing_id = int(listing_id)
            if listing_id <= 0:
                raise ValueError
            remove_favorite(user["id"], listing_id)
        except (ValueError, TypeError):
            self.send_json({"error": "Invalid listing."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"listing_id": listing_id})

    def handle_listing_create(self, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            values = json.loads(body)
            title = str(values.get("title", "")).strip()
            category = " ".join(
                word[:1].upper() + word[1:].lower()
                for word in str(values.get("category", "")).strip().split()
            )
            item_condition = str(values.get("condition", "")).strip()
            description = str(values.get("description", "")).strip()
            price = float(values.get("price", 0))
            image_data = str(values.get("image_data", "")).strip()
            image_url = self.save_listing_image(image_data)
            if not title or not category or not item_condition or not description or price < 0:
                raise ValueError
            listing_id = create_listing(
                user["id"], title, category, item_condition, description, price, image_url
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json({"error": "Complete all fields and enter a valid price."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"id": listing_id}, HTTPStatus.CREATED)

    def handle_listing_update(self, listing_id, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            listing_id = int(listing_id)
            values = json.loads(body)
            title = str(values.get("title", "")).strip()
            category = " ".join(
                word[:1].upper() + word[1:].lower()
                for word in str(values.get("category", "")).strip().split()
            )
            item_condition = str(values.get("condition", "")).strip()
            description = str(values.get("description", "")).strip()
            price = float(values.get("price", 0))
            image_data = str(values.get("image_data", "")).strip()
            image_url = self.save_listing_image(image_data) if image_data else None
            if not title or not category or not item_condition or not description or price < 0:
                raise ValueError
            updated = update_listing(
                user["id"], listing_id, title, category, item_condition, description, price, image_url
            )
            if not updated:
                self.send_json({"error": "Listing was not found."}, HTTPStatus.NOT_FOUND)
                return
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json({"error": "Complete all fields and enter a valid price."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"id": listing_id})

    @staticmethod
    def save_listing_image(image_data):
        if not image_data:
            return ""
        try:
            header, encoded_image = image_data.split(",", 1)
            mime_type = header.removeprefix("data:").split(";", 1)[0]
            if mime_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                raise ValueError
            image_bytes = base64.b64decode(encoded_image, validate=True)
        except (ValueError, TypeError, base64.binascii.Error):
            raise ValueError from None
        if len(image_bytes) > 5 * 1024 * 1024:
            raise ValueError
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}[mime_type]
        upload_directory = ROOT / "static" / "uploads"
        upload_directory.mkdir(parents=True, exist_ok=True)
        filename = f"listing-{secrets.token_urlsafe(12)}{extension}"
        (upload_directory / filename).write_bytes(image_bytes)
        return f"/static/uploads/{filename}"

    def send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_reset_request(self, values):
        email = values.get("email", [""])[0].strip().lower()
        generic_message = "If an account exists for that email, a reset link has been sent."
        try:
            user = find_user_by_email(email)
            if user:
                token = secrets.token_urlsafe(32)
                RESET_TOKENS[token] = {"email": email, "expires_at": time.time() + 900}
                try:
                    self.send_reset_email(email, token)
                except (OSError, smtplib.SMTPException):
                    RESET_TOKENS.pop(token, None)
                    raise
        except (Error, OSError, smtplib.SMTPException) as error:
            if "SMTP credentials are not configured" in str(error):
                message = "Reset email is not configured. Add your Gmail address and Gmail App Password to the root .env file."
            else:
                message = "Gmail could not send the reset email. Check your Gmail App Password and SMTP settings."
            self.redirect_with_message(
                "/templates/reset-password.html",
                message,
                "error",
            )
            return
        self.redirect_with_message("/templates/reset-password.html", generic_message, "message")

    def handle_reset_password(self, values):
        token = values.get("token", [""])[0]
        reset_data = RESET_TOKENS.get(token)
        password = values.get("password", [""])[0]
        confirmation = values.get("confirm_password", [""])[0]
        strong_password = (
            len(password) >= 8
            and re.search(r"[a-z]", password)
            and re.search(r"[A-Z]", password)
            and re.search(r"\d", password)
            and re.search(r"[^A-Za-z\d]", password)
        )
        if not reset_data or reset_data["expires_at"] < time.time():
            self.redirect_with_message("/templates/reset-password.html", "This reset link is invalid or expired.", "error")
            return
        if not strong_password:
            self.redirect_with_message(
                "/reset-password",
                "Password must have at least 8 characters, one uppercase letter, one lowercase letter, one number, and one special character.",
                "error",
                {"token": token},
            )
            return
        if password != confirmation:
            self.redirect_with_message("/reset-password", "Passwords do not match.", "error", {"token": token})
            return
        try:
            update_user_password(reset_data["email"], hash_password(password))
        except Error:
            self.redirect_with_message("/reset-password", "Database connection failed.", "error", {"token": token})
            return
        RESET_TOKENS.pop(token, None)
        self.redirect_with_message(
            "/templates/login.html",
            "Your password has been reset successfully. You can now log in.",
            "message",
            {"reset_success": "1"},
        )

    @staticmethod
    def send_reset_email(email, token):
        username = os.getenv("NCART_SMTP_USERNAME", "")
        password = os.getenv("NCART_SMTP_PASSWORD", "").replace(" ", "")
        if not username or not password:
            raise OSError("SMTP credentials are not configured")
        base_url = os.getenv("NCART_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        reset_url = f"{base_url}/reset-password?{urlencode({'token': token})}"
        message = EmailMessage()
        message["Subject"] = "Reset your NCart password"
        message["From"] = os.getenv("NCART_SMTP_FROM", username)
        message["To"] = email
        message.set_content(
            "We received a request to reset your NCart password.\n\n"
            f"Use this link within 15 minutes:\n{reset_url}\n\n"
            "If you did not request this, you can ignore this email."
        )
        with smtplib.SMTP(os.getenv("NCART_SMTP_HOST", "smtp.gmail.com"), int(os.getenv("NCART_SMTP_PORT", "587")), timeout=10) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)

    @staticmethod
    def send_order_status_email(order):
        username = os.getenv("NCART_SMTP_USERNAME", "")
        password = os.getenv("NCART_SMTP_PASSWORD", "").replace(" ", "")
        if not username or not password:
            return
        status = str(order.get("status", "pending")).replace("_", " ").title()
        message = EmailMessage()
        message["Subject"] = f"NCart order update: {status}"
        message["From"] = os.getenv("NCART_SMTP_FROM", username)
        message["To"] = order["buyer_email"]
        message.set_content(
            f"Hi {order.get('buyer_name', 'there')},\n\n"
            f"Your NCart order #{order['id']} for {order['listing_title']} is now {status}.\n\n"
            "You can open My Orders in NCart to track the latest delivery status.\n\n"
            "Thank you,\nNCart"
        )
        with smtplib.SMTP(
            os.getenv("NCART_SMTP_HOST", "smtp.gmail.com"),
            int(os.getenv("NCART_SMTP_PORT", "587")),
            timeout=10,
        ) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)

    def handle_login(self, values):
        email = values.get("email", [""])[0].strip().lower()
        password = values.get("password", [""])[0]
        try:
            user = find_user_by_email(email)
            valid = user is not None and verify_password(password, user["password_hash"])
        except Error:
            self.redirect_with_message("/templates/login.html", "Database connection failed.", "error")
            return

        if user is None:
            self.redirect_with_message(
                "/templates/create-account.html",
                "No NCart account was found for this email. Create an account first.",
                "error",
            )
            return
        if not valid:
            self.redirect_with_message("/templates/login.html", "Invalid email or password.", "error")
            return
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = {"id": int(user["id"]), "email": user["email"]}
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/templates/login.html?message=Login+successful.+Welcome+back.&login_success=1")
        self.send_header("Set-Cookie", f"ncart_session={make_session_cookie(user['id'])}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()

    def current_user(self):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookies.get("ncart_session")
        if not token:
            return None
        session = SESSIONS.get(token.value)
        if session:
            return session
        session = read_signed_session(token.value)
        if not session:
            return None
        try:
            return find_user_by_id(session["id"])
        except Error:
            return None

    def handle_logout(self):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookies.get("ncart_session")
        if token:
            SESSIONS.pop(token.value, None)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", "ncart_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        self.end_headers()

    def handle_register(self, values):
        google_token = values.get("google_token", [""])[0]
        google_registration = GOOGLE_REGISTRATIONS.get(google_token)
        if google_token:
            if not google_registration or google_registration["expires_at"] < time.time():
                self.redirect_with_message(
                    "/templates/login.html",
                    "Your Google registration expired. Please sign in with Google again.",
                    "error",
                )
                return

        surname = values.get("surname", [""])[0].strip()
        first_name = values.get("first_name", [""])[0].strip()
        middle_name = values.get("middle_name", [""])[0].strip()
        no_middle_name = values.get("no_middle_name", [""])[0] == "1"
        email = values.get("email", [""])[0].strip().lower()
        password = values.get("password", [""])[0]
        confirmation = values.get("confirm_password", [""])[0]

        if google_registration:
            email = google_registration["email"]

        if no_middle_name:
            middle_name = ""
        full_name = " ".join(part for part in (first_name, middle_name, surname) if part)

        strong_password = (
            len(password) >= 8
            and re.search(r"[a-z]", password)
            and re.search(r"[A-Z]", password)
            and re.search(r"\d", password)
            and re.search(r"[^A-Za-z\d]", password)
        )
        if not surname or not first_name or (not middle_name and not no_middle_name):
            self.redirect_with_message(
                "/templates/create-account.html",
                "Enter your surname, first name, and middle name, or check that you have no middle name.",
                "error",
            )
            return
        if "@" not in email:
            self.redirect_with_message(
                "/templates/create-account.html",
                "Enter a valid email address.",
                "error",
            )
            return
        if not strong_password:
            self.redirect_with_message(
                "/templates/create-account.html",
                "Password must have at least 8 characters, one uppercase letter, one lowercase letter, one number, and one special character (for example: Ncart2026!).",
                "error",
            )
            return
        if password != confirmation:
            self.redirect_with_message("/templates/create-account.html", "Passwords do not match.", "error")
            return

        try:
            create_user(full_name, email, hash_password(password))
            if google_registration:
                user = find_user_by_email(email)
                token = secrets.token_urlsafe(32)
                SESSIONS[token] = {"id": int(user["id"]), "email": user["email"]}
                GOOGLE_REGISTRATIONS.pop(google_token, None)
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/dashboard")
                self.send_header("Set-Cookie", f"ncart_session={make_session_cookie(user['id'])}; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
                return
        except IntegrityError:
            self.redirect_with_message("/templates/create-account.html", "An account with that email already exists.", "error")
            return
        except Error:
            self.redirect_with_message("/templates/create-account.html", "Database connection failed.", "error")
            return

        self.redirect_with_message("/templates/login.html", "Account created. You can now log in.", "message")

    def redirect_with_message(self, path, message, kind, extra=None):
        parameters = {kind: message}
        if extra:
            parameters.update(extra)
        query = urlencode(parameters)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"{path}?{query}")
        self.end_headers()

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")


if __name__ == "__main__":
    initialize_database()
    server = ThreadingHTTPServer(("127.0.0.1", 8000), NCartHandler)
    print("NCart is running at http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping NCart")
    finally:
        server.server_close()
