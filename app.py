import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import smtplib
import time
from http.cookies import SimpleCookie
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from mysql.connector import Error, IntegrityError

from db import (
    add_favorite,
    add_cart_item,
    cancel_order,
    count_cart_items,
    create_listing,
    create_message,
    count_unread_messages,
    create_order,
    ORDER_PAYMENT_METHODS,
    create_transaction_rating,
    create_user,
    create_pending_admin_registration,
    claim_admin_registration_otp_delivery,
    update_admin_otp_delivery_status,
    find_listings,
    find_marketplace_listings,
    find_marketplace_listing_by_id,
    find_messages,
    find_orders_for_user,
    find_reports_for_user,
    find_admin_reports,
    find_admin_report_detail,
    find_report_summary,
    find_order_status_notification,
    find_order_payment_notification,
    find_order_payment_receipt,
    find_sales_for_user,
    find_user_thread,
    find_user_messages,
    mark_messages_read,
    find_notifications_for_user,
    count_unread_notifications,
    mark_notifications_read,
    find_favorite_ids,
    find_favorite_listings,
    find_cart_items,
    find_user_by_email,
    find_user_by_id,
    find_public_profile,
    find_public_user_identity,
    initialize_database,
    update_user_password,
    update_user_profile,
    update_listing,
    update_order_status,
    remove_favorite,
    remove_cart_item,
    update_cart_item_quantity,
    create_report,
    find_pending_student_verifications,
    find_student_verification_image,
    find_pending_accounts,
    find_user_by_student_number,
    resubmit_student_verification,
    review_pending_account,
    review_student_verification,
    review_report,
    review_order_payment,
    submit_order_payment,
    issue_admin_registration_otp,
    complete_admin_registration,
    invalidate_admin_registration_otp,
    compose_full_name,
    normalize_name_part,
)

ROOT = Path(__file__).parent
logger = logging.getLogger(__name__)
CATEGORY_OPTIONS = (
    "Books",
    "Clothing",
    "Electronics",
    "School Supplies",
    "Accessories",
    "Bags",
    "Others",
)
PRIVATE_STUDENT_ID_DIRECTORY = ROOT / "private" / "student_ids"
MAX_STUDENT_ID_IMAGE_SIZE = 5 * 1024 * 1024
MAX_REGISTRATION_BODY_SIZE = MAX_STUDENT_ID_IMAGE_SIZE + 128 * 1024
PRIVATE_PAYMENT_RECEIPT_DIRECTORY = ROOT.parent / ".ncart-private" / "payment_receipts"
MAX_PAYMENT_RECEIPT_SIZE = 5 * 1024 * 1024
MAX_PAYMENT_BODY_SIZE = MAX_PAYMENT_RECEIPT_SIZE + 64 * 1024
MAX_MESSAGE_IMAGE_SIZE = 5 * 1024 * 1024
MAX_MESSAGE_BODY_SIZE = 7 * 1024 * 1024
ADMIN_REGISTRATION_OTP_TTL = 5 * 60
ADMIN_REGISTRATION_OTP_COOLDOWN = 60
load_dotenv(ROOT / ".env")
SESSION_SECRET = os.getenv("NCART_SESSION_SECRET") or secrets.token_urlsafe(32)
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_STATES = {}
GOOGLE_REGISTRATIONS = {}
RESET_TOKENS = {}


def normalize_listing_category(value):
    category = str(value or "").strip()
    if category.casefold() in {"add category", "__add_category__"}:
        return "Others"
    return next(
        (option for option in CATEGORY_OPTIONS if option.casefold() == category.casefold()),
        "",
    )


SESSIONS = {}


def parse_registration_multipart(content_type, body):
    try:
        headers = (
            b"Content-Type: "
            + content_type.encode("ascii")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
        )
        message = BytesParser(policy=default).parsebytes(headers + body)
    except (UnicodeEncodeError, ValueError):
        raise ValueError("Invalid registration form data.") from None
    if message.get_content_type() != "multipart/form-data" or not message.is_multipart():
        raise ValueError("Registration must use a multipart form.")

    values = {}
    student_id_image = None
    student_id_content_type = ""
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if name == "student_id_image":
            if student_id_image is not None:
                raise ValueError("Invalid uploaded file.")
            if not filename and not payload:
                continue
            if not filename:
                raise ValueError("Invalid uploaded file.")
            student_id_image = payload
            student_id_content_type = part.get_content_type()
            continue
        if filename is not None:
            raise ValueError("Invalid uploaded file.")
        try:
            value = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Invalid registration form data.") from None
        values.setdefault(name, []).append(value)

    return values, student_id_image, student_id_content_type


def identify_student_id_image(image_bytes):
    if not image_bytes or len(image_bytes) > MAX_STUDENT_ID_IMAGE_SIZE:
        raise ValueError("Student ID images must be no larger than 5 MB.")
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if (
        len(image_bytes) >= 12
        and image_bytes.startswith(b"RIFF")
        and image_bytes[8:12] == b"WEBP"
    ):
        return "image/webp", ".webp"
    raise ValueError("Upload a valid JPG, PNG, or WebP image of your Student ID.")


def save_student_id_image(image_bytes, extension):
    PRIVATE_STUDENT_ID_DIRECTORY.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(24)}{extension}"
    path = PRIVATE_STUDENT_ID_DIRECTORY / filename
    path.write_bytes(image_bytes)
    return path.relative_to(ROOT).as_posix()


def remove_student_id_image(relative_path):
    if not relative_path:
        return
    path = (ROOT / relative_path).resolve()
    private_directory = PRIVATE_STUDENT_ID_DIRECTORY.resolve()
    if path.is_relative_to(private_directory) and path.is_file():
        path.unlink()


def parse_payment_multipart(content_type, body, require_amount=True):
    try:
        headers = (
            b"Content-Type: "
            + content_type.encode("ascii")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
        )
        message = BytesParser(policy=default).parsebytes(headers + body)
    except (UnicodeEncodeError, ValueError):
        raise ValueError("Invalid payment submission.") from None
    if message.get_content_type() != "multipart/form-data" or not message.is_multipart():
        raise ValueError("Submit the payment using the form.")

    values = {}
    receipt = None
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if name == "payment_receipt":
            if receipt is not None:
                raise ValueError("Upload one payment receipt.")
            if not filename or not payload:
                raise ValueError("Upload a payment receipt image.")
            if len(payload) > MAX_PAYMENT_RECEIPT_SIZE:
                raise ValueError("Payment receipts must be no larger than 5 MB.")
            receipt = payload
            continue
        if filename is not None:
            raise ValueError("Invalid payment submission.")
        try:
            value = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Invalid payment submission.") from None
        if name in values:
            raise ValueError("Invalid payment submission.")
        values[name] = value

    if receipt is None:
        raise ValueError("Upload a payment receipt image.")
    reference_no = values.get("payment_reference_no", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/#-]{0,99}", reference_no):
        raise ValueError("Enter a valid payment reference number.")
    payment_amount = values.get("payment_amount", "").strip()
    if require_amount and not re.fullmatch(r"\d{1,8}(?:\.\d{1,2})?", payment_amount):
        raise ValueError("The payment amount does not match the order total.")
    if payment_amount and not re.fullmatch(r"\d{1,8}(?:\.\d{1,2})?", payment_amount):
        raise ValueError("The payment amount does not match the order total.")
    if receipt.startswith(b"\xff\xd8\xff"):
        extension = ".jpg"
    elif receipt.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = ".png"
    else:
        raise ValueError("Upload a valid JPG, JPEG, or PNG receipt image.")
    return values, reference_no, payment_amount, receipt, extension


def save_payment_receipt(image_bytes, extension):
    PRIVATE_PAYMENT_RECEIPT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(24)}{extension}"
    path = PRIVATE_PAYMENT_RECEIPT_DIRECTORY / filename
    try:
        path.write_bytes(image_bytes)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return filename


def remove_payment_receipt(filename):
    if not filename or not re.fullmatch(r"[a-f0-9]{48}\.(?:jpg|png)", filename):
        return
    path = PRIVATE_PAYMENT_RECEIPT_DIRECTORY / filename
    if path.is_file():
        path.unlink()


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


def parse_quantity(value, minimum=0):
    if isinstance(value, bool):
        raise ValueError
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError from error
    if not math.isfinite(number) or not number.is_integer() or number < minimum or number > 4294967295:
        raise ValueError
    return int(number)


class NCartHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        route = urlparse(self.path).path
        protected_pages = {
            "/admin",
            "/home",
            "/dashboard",
            "/profile",
            "/listings",
            "/messages",
            "/notifications",
            "/orders",
            "/favorites",
            "/cart",
            "/reports",
            "/templates/admin.html",
            "/templates/home.html",
            "/templates/profile.html",
            "/templates/listings.html",
            "/templates/messages.html",
            "/templates/notifications.html",
            "/templates/orders.html",
            "/templates/favorites.html",
            "/templates/cart.html",
            "/templates/reports.html",
        }
        if route in protected_pages:
            account = self.current_account()
            if account:
                account_is_active = (
                    account.get("status") == "active"
                    and account.get("account_status") in {"active", "verified"}
                    and account.get("approval_status") == "approved"
                )
                if not account_is_active:
                    if account.get("role") != "admin":
                        account_state = account.get("status")
                        if account_state not in {"pending", "rejected"}:
                            account_state = "pending"
                        destination = f"/pending-account?state={account_state}"
                    else:
                        destination = (
                            "/templates/login.html?error="
                            "This+Admin+account+is+not+active.+Contact+the+NCart+system+owner."
                        )
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Location", destination)
                    self.end_headers()
                    return
        order_route = route.removeprefix("/api/orders/").split("/")
        if (
            route.startswith("/api/orders/")
            and len(order_route) == 2
            and order_route[1] == "payment-receipt"
        ):
            self.handle_order_payment_receipt(order_route[0])
            return
        if route.startswith("/api/admin/") and not self.is_approved_admin(self.current_user()):
            self.send_admin_access_denied()
            return
        admin_template = (ROOT / "templates" / "admin.html").resolve()
        requested_file = Path(self.translate_path(self.path)).resolve()
        private_directory = PRIVATE_STUDENT_ID_DIRECTORY.resolve()
        if requested_file == private_directory or private_directory in requested_file.parents:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if requested_file == admin_template:
            user = self.current_user()
            if not self.is_approved_admin(user):
                self.send_admin_access_denied()
                return
        route = self.path.split("?", 1)[0]
        if route == "/api/reports":
            self.handle_reports_get()
            return
        if route == "/api/admin/reports/summary":
            self.handle_admin_report_summary()
            return
        if route == "/api/admin/reports":
            self.handle_admin_reports_get()
            return
        if route.startswith("/api/admin/reports/"):
            report_route = route.removeprefix("/api/admin/reports/").split("/")
            if len(report_route) == 2 and report_route[1] == "history":
                self.handle_admin_report_history(report_route[0])
                return
            if len(report_route) == 1:
                self.handle_admin_report_detail(report_route[0])
                return
        if route == "/api/admin/student-verifications":
            self.handle_admin_student_verifications_get()
            return
        if route == "/api/admin/account-confirmations":
            self.handle_pending_accounts_get()
            return
        if route.startswith("/api/admin/student-verifications/"):
            verification_route = route.removeprefix("/api/admin/student-verifications/").split("/")
            if len(verification_route) == 2 and verification_route[1] == "image":
                self.handle_admin_student_verification_image(verification_route[0])
                return
        if route == "/logout":
            self.handle_logout()
            return
        if route == "/api/listings":
            self.handle_listings_get()
            return
        if route.startswith("/api/users/") and route.endswith("/profile"):
            profile_user_id = route.removeprefix("/api/users/").removesuffix("/profile").strip("/")
            self.handle_public_profile_get(profile_user_id)
            return
        if route == "/api/profile":
            self.handle_profile_get()
            return
        if route == "/api/messages":
            self.handle_messages_get()
            return
        if route == "/api/notifications":
            self.handle_notifications_get()
            return
        if route == "/api/favorites":
            self.handle_favorites_get()
            return
        if route == "/api/cart":
            self.handle_cart_get()
            return
        if route == "/api/cart/count":
            self.handle_cart_count()
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
        if route in {"/admin", "/templates/admin.html"}:
            user = self.current_user()
            if not self.is_approved_admin(user):
                self.send_admin_access_denied()
                return
            self.path = "/templates/admin.html"
        elif route == "/reports":
            user = self.current_user()
            if user is None:
                self.redirect_with_message("/templates/login.html", "Please log in to view reports.", "error")
                return
            self.path = "/templates/reports.html"
        elif route == "/":
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
        elif route == "/pending-account":
            self.path = "/templates/pending-account.html"
        elif route.startswith("/users/"):
            profile_user_id = route.removeprefix("/users/").strip("/")
            if not profile_user_id.isascii() or not profile_user_id.isdecimal() or int(profile_user_id) < 1:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.path = f"/templates/public-profile.html?user_id={int(profile_user_id)}"
        elif route in {
            "/marketplace",
            "/favorites",
            "/listings",
            "/messages",
            "/notifications",
            "/orders",
            "/reports",
            "/profile",
            "/cart",
        }:
            query = urlparse(self.path).query
            self.path = f"/templates{route}.html" + (f"?{query}" if query else "")
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
                "prompt": "select_account",
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
            if user.get("role") != "admin" and user.get("status") == "pending":
                self.redirect_with_message(
                    "/pending-account",
                    "Your account is currently pending approval.",
                    "message",
                    {"state": "pending"},
                )
                return
            if user.get("role") != "admin" and user.get("status") == "rejected":
                self.redirect_with_message(
                    "/pending-account",
                    "Your account has not been approved by the administrator.",
                    "error",
                    {"state": "rejected"},
                )
                return
            if user.get("role") == "admin" and (
                user.get("status") != "active"
                or user.get("account_status") not in {"active", "verified"}
                or user.get("approval_status") != "approved"
            ):
                self.redirect_with_message(
                    "/templates/login.html",
                    "This Admin account is not active. Contact the NCart system owner.",
                    "error",
                )
                return
            if user.get("account_status") == "pending_verification":
                self.redirect_with_message(
                    "/templates/login.html",
                    "Your Student ID is pending administrator verification.",
                    "message",
                )
                return
            if user.get("account_status") == "rejected":
                self.redirect_with_message(
                    "/templates/create-account.html",
                    "Your Student ID could not be verified. Log in with your password to submit another valid ID.",
                    "error",
                    {"email": email, "retry_verification": "1"},
                )
                return
            if user.get("account_status") not in {"active", "verified"}:
                self.redirect_with_message("/templates/login.html", "This account is suspended. Contact NCart support.", "error")
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
        if route == "/api/cart":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 16 * 1024:
                    raise ValueError
                body = self.rfile.read(length).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                self.send_json({"error": "Invalid cart request."}, HTTPStatus.BAD_REQUEST)
                return
            self.handle_cart_add(body)
            return
        if route == "/api/orders":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                content_type = self.headers.get_content_type()
                if content_type == "application/json":
                    if length <= 0 or length > 16 * 1024:
                        raise ValueError
                    body = self.rfile.read(length).decode("utf-8")
                    self.handle_order_create(body)
                    return
                if content_type != "multipart/form-data" or length <= 0 or length > MAX_PAYMENT_BODY_SIZE:
                    raise ValueError
                values, reference_no, _, receipt_bytes, extension = parse_payment_multipart(
                    self.headers.get("Content-Type", ""),
                    self.rfile.read(length),
                    require_amount=False,
                )
            except (ValueError, UnicodeDecodeError) as error:
                message = str(error) or "Invalid order request."
                self.send_json({"error": message}, HTTPStatus.BAD_REQUEST)
                return
            except OSError:
                self.send_json({"error": "Invalid order request."}, HTTPStatus.BAD_REQUEST)
                return
            self.handle_order_create(
                json.dumps(values),
                {"reference_no": reference_no, "receipt_bytes": receipt_bytes, "extension": extension},
            )
            return
        if route.startswith("/api/admin/") and not self.is_approved_admin(self.current_user()):
            self.send_admin_access_denied()
            return
        if route == "/api/messages":
            if self.current_user() is None:
                self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json({"error": "Invalid message request."}, HTTPStatus.BAD_REQUEST)
                return
            if length <= 0 or length > MAX_MESSAGE_BODY_SIZE:
                self.send_json(
                    {"error": "Message attachments must be no larger than 5 MB."},
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            if self.headers.get_content_type() != "application/json":
                self.send_json({"error": "Submit the message as JSON."}, HTTPStatus.BAD_REQUEST)
                return
            try:
                body = self.rfile.read(length).decode("utf-8")
            except UnicodeDecodeError:
                self.send_json({"error": "Invalid message request."}, HTTPStatus.BAD_REQUEST)
                return
            self.handle_message_create(body)
            return
        if route.startswith("/api/orders/"):
            order_route = route.removeprefix("/api/orders/").split("/")
            if len(order_route) == 2 and order_route[1] in {"payment", "payment-review"}:
                self.handle_order_payment_post(order_route[0], order_route[1])
                return
            if len(order_route) == 2 and order_route[1] == "rating":
                self.handle_order_rating_create(order_route[0])
                return
        if route == "/api/admin-registration/send-otp":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json({"error": "Invalid registration form data."}, HTTPStatus.BAD_REQUEST)
                return
            if length <= 0 or length > MAX_REGISTRATION_BODY_SIZE:
                self.send_json(
                    {"error": "Registration details are too large."},
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            if self.headers.get_content_type() != "multipart/form-data":
                self.send_json({"error": "Submit the account registration form."}, HTTPStatus.BAD_REQUEST)
                return
            try:
                values, student_id_image, image_content_type = parse_registration_multipart(
                    self.headers.get("Content-Type", ""),
                    self.rfile.read(length),
                )
            except ValueError as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.handle_admin_registration_send_otp(
                values,
                student_id_image,
                image_content_type,
            )
            return
        if route in {
            "/api/admin-registration/resend-otp",
            "/api/admin-registration/verify-otp",
        }:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json({"error": "Invalid verification request."}, HTTPStatus.BAD_REQUEST)
                return
            if length <= 0 or length > 16 * 1024:
                self.send_json({"error": "Verification request is too large."}, HTTPStatus.BAD_REQUEST)
                return
            try:
                values = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_json({"error": "Invalid verification request."}, HTTPStatus.BAD_REQUEST)
                return
            if route.endswith("/resend-otp"):
                self.handle_admin_registration_resend_otp(values)
            else:
                self.handle_admin_registration_verify_otp(values)
            return
        if route == "/register":
            self.redirect_with_message(
                "/templates/create-account.html",
                "Verify your email with the one-time code to create your account.",
                "error",
            )
            return
        if route.startswith("/api/admin/account-confirmations/") and route.endswith("/review"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 16 * 1024:
                    raise ValueError
                values = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self.send_json({"error": "Invalid account decision."}, HTTPStatus.BAD_REQUEST)
                return
            user_id = route.removeprefix(
                "/api/admin/account-confirmations/"
            ).removesuffix("/review").strip("/")
            self.handle_pending_account_review(user_id, values)
            return
        if route.startswith("/api/admin/account-confirmations/") and route.endswith("/retry-otp"):
            user_id = route.removeprefix(
                "/api/admin/account-confirmations/"
            ).removesuffix("/retry-otp").strip("/")
            self.handle_admin_registration_otp_retry(user_id)
            return
        if route.startswith("/api/admin/student-verifications/") and route.endswith("/review"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json({"error": "Invalid verification decision."}, HTTPStatus.BAD_REQUEST)
                return
            if length < 0 or length > 16 * 1024:
                self.send_json({"error": "Verification decision is too large."}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            try:
                body = self.rfile.read(length).decode("utf-8")
            except UnicodeDecodeError:
                self.send_json({"error": "Invalid verification decision."}, HTTPStatus.BAD_REQUEST)
                return
            user_id = route.removeprefix("/api/admin/student-verifications/").removesuffix("/review").strip("/")
            self.handle_admin_student_verification_review(user_id, body)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        if route == "/api/reports":
            self.handle_report_create(body)
            return
        if route.startswith("/api/admin/reports/") and route.endswith("/review"):
            report_id = route.removeprefix("/api/admin/reports/").removesuffix("/review").strip("/")
            self.handle_admin_report_review(report_id, body)
            return
        if route == "/api/listings":
            self.handle_listing_create(body)
            return
        if route == "/api/profile":
            self.handle_profile_update(body)
            return
        if route == "/api/notifications/mark-read":
            self.handle_notifications_mark_read(body)
            return
        if route == "/api/messages":
            self.handle_message_create(body)
            return
        if route == "/api/favorites":
            self.handle_favorite_create(body)
            return
        values = parse_qs(body, keep_blank_values=True)

        if route == "/login":
            self.handle_login(values)
        elif route == "/reset-password":
            self.handle_reset_password(values)
        elif route == "/request-reset":
            self.handle_reset_request(values)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        route = urlparse(self.path).path
        if route.startswith("/api/admin/") and not self.is_approved_admin(self.current_user()):
            self.send_admin_access_denied()
            return
        if route.startswith("/api/orders/"):
            length = int(self.headers.get("Content-Length", "0"))
            self.handle_order_status_update(route.removeprefix("/api/orders/"), self.rfile.read(length).decode())
            return
        if route == "/api/profile":
            length = int(self.headers.get("Content-Length", "0"))
            self.handle_profile_update(self.rfile.read(length).decode())
            return
        if route.startswith("/api/cart/"):
            length = int(self.headers.get("Content-Length", "0"))
            self.handle_cart_update(route.removeprefix("/api/cart/"), self.rfile.read(length).decode())
            return
        if not route.startswith("/api/listings/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.handle_listing_update(route.removeprefix("/api/listings/"), self.rfile.read(length).decode())

    def do_DELETE(self):
        route = urlparse(self.path).path
        if route.startswith("/api/admin/") and not self.is_approved_admin(self.current_user()):
            self.send_admin_access_denied()
            return
        if route.startswith("/api/favorites/"):
            self.handle_favorite_delete(route.removeprefix("/api/favorites/"))
            return
        if route.startswith("/api/cart/"):
            self.handle_cart_remove(route.removeprefix("/api/cart/"))
            return
        if route.startswith("/api/orders/"):
            self.handle_order_cancel(route.removeprefix("/api/orders/"))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PATCH(self):
        route = urlparse(self.path).path
        if not route.startswith("/api/cart/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16 * 1024:
                raise ValueError
            body = self.rfile.read(length).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            self.send_json({"error": "Invalid cart request."}, HTTPStatus.BAD_REQUEST)
            return
        self.handle_cart_update(route.removeprefix("/api/cart/"), body)

    def handle_listings_get(self):
        user = self.current_user()
        query = parse_qs(urlparse(self.path).query)
        mine = query.get("mine", [""])[0] == "1"
        if mine and user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        if not mine and "listing" in query:
            try:
                listing_id = int(query["listing"][0])
                if listing_id < 1:
                    raise ValueError
                listing = find_marketplace_listing_by_id(listing_id)
            except (ValueError, IndexError):
                self.send_json({"error": "Invalid listing."}, HTTPStatus.BAD_REQUEST)
                return
            except Error:
                self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if listing is None:
                self.send_json({"error": "Listing is no longer available."}, HTTPStatus.NOT_FOUND)
                return
            listing["id"] = int(listing["id"])
            listing["seller_id"] = int(listing["seller_id"])
            listing["quantity"] = int(listing["quantity"])
            listing["price"] = str(listing["price"])
            listing["created_at"] = listing["created_at"].isoformat()
            self.send_json(
                {
                    "viewer_id": int(user["id"]) if user else None,
                    "listings": [listing],
                    "categories": [listing["category"]],
                    "pagination": {
                        "page": 1, "per_page": 1,
                        "total_items": 1, "total_pages": 1,
                    },
                }
            )
            return
        if not mine and "page" in query:
            try:
                page = int(query["page"][0])
                if page < 1:
                    raise ValueError
                category = query.get("category", [""])[0].strip()
                search = query.get("search", [""])[0].strip()
                if len(category) > 80 or len(search) > 200:
                    raise ValueError
                listings, total_count, categories = find_marketplace_listings(
                    page, 6, category, search
                )
            except (ValueError, IndexError):
                self.send_json({"error": "Invalid marketplace page or filter."}, HTTPStatus.BAD_REQUEST)
                return
            except Error:
                self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            total_pages = (total_count + 5) // 6
            if page > max(total_pages, 1):
                page = max(total_pages, 1)
                listings, total_count, categories = find_marketplace_listings(
                    page, 6, category, search
                )
                total_pages = (total_count + 5) // 6
            for listing in listings:
                listing["id"] = int(listing["id"])
                listing["seller_id"] = int(listing["seller_id"])
                listing["quantity"] = int(listing["quantity"])
                listing["price"] = str(listing["price"])
                listing["created_at"] = listing["created_at"].isoformat()
            self.send_json(
                {
                    "viewer_id": int(user["id"]) if user else None,
                    "listings": listings,
                    "categories": categories,
                    "pagination": {
                        "page": page,
                        "per_page": 6,
                        "total_items": total_count,
                        "total_pages": total_pages,
                    },
                }
            )
            return
        try:
            listings = find_listings(user["id"] if mine else None)
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        for listing in listings:
            listing["id"] = int(listing["id"])
            listing["quantity"] = int(listing["quantity"])
            listing["price"] = str(listing["price"])
            listing["created_at"] = listing["created_at"].isoformat()
        self.send_json({"viewer_id": int(user["id"]) if user else None, "listings": listings})

    def handle_profile_get(self):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        self.send_json({"profile": user})

    def handle_public_profile_get(self, profile_user_id):
        try:
            parsed_user_id = int(profile_user_id)
            if parsed_user_id < 1:
                raise ValueError
            profile = find_public_profile(parsed_user_id)
        except ValueError:
            self.send_json({"error": "Profile not found."}, HTTPStatus.NOT_FOUND)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if profile is None:
            self.send_json({"error": "Profile not found."}, HTTPStatus.NOT_FOUND)
            return
        public_listings = []
        for listing in profile["listings"]:
            quantity = int(listing["quantity"])
            public_listings.append(
                {
                    "title": listing["title"],
                    "price": str(listing["price"]),
                    "condition": listing["item_condition"],
                    "image_url": listing["image_url"],
                    "quantity": quantity,
                    "status": "Available" if quantity > 0 else "Out of stock",
                    "created_at": listing["created_at"].isoformat(),
                }
            )
        public_reviews = [
            {
                "rating": int(review["rating"]),
                "review": review["review"],
                "created_at": review["created_at"].isoformat(),
            }
            for review in profile["reviews"]
        ]
        average_rating = profile["average_rating"]
        self.send_json(
            {
                "profile": {
                    "full_name": profile["full_name"],
                    "profile_image_url": profile["profile_image_url"],
                    "verified": bool(profile["verified"]),
                    "post_count": int(profile["post_count"]),
                    "average_rating": round(float(average_rating), 1) if average_rating is not None else None,
                    "rating_count": int(profile["rating_count"]),
                    "completed_transaction_count": int(profile["completed_transaction_count"]),
                    "created_at": profile["created_at"].isoformat(),
                },
                "listings": public_listings,
                "reviews": public_reviews,
            }
        )

    def handle_order_rating_create(self, order_id):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            parsed_order_id = int(order_id)
            length = int(self.headers.get("Content-Length", "0"))
            if parsed_order_id < 1 or length <= 0 or length > 16 * 1024:
                raise ValueError
            values = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(values, dict):
                raise ValueError
            rating = values.get("rating")
            review = values.get("review", "")
            if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
                raise ValueError
            if not isinstance(review, str) or len(review.strip()) > 1000:
                raise ValueError
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"error": "Choose a rating from 1 to 5 and enter a review of 1,000 characters or fewer."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            created, error = create_transaction_rating(
                user["id"],
                parsed_order_id,
                rating,
                review.strip(),
            )
        except IntegrityError as error:
            if error.errno == 1062:
                self.send_json({"error": "You have already rated this transaction."}, HTTPStatus.CONFLICT)
                return
            self.send_json({"error": "Could not save your rating."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not created:
            self.send_json({"error": error}, HTTPStatus.CONFLICT)
            return
        self.send_json({"message": "Your rating has been submitted."}, HTTPStatus.CREATED)

    def handle_profile_update(self, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            values = json.loads(body)
            if not isinstance(values, dict):
                raise ValueError
            if not isinstance(values, dict):
                raise ValueError
            full_name = str(values.get("full_name", "")).strip()
            bio = str(values.get("bio", "")).strip()
            location = str(values.get("location", "")).strip()
            username = str(values.get("username", "")).strip().lower()
            image_data = str(values.get("image_data", "")).strip()
            cover_image_data = str(values.get("cover_image_data", "")).strip()
            if (
                not full_name
                or len(full_name) > 120
                or len(bio) > 500
                or len(location) > 160
                or not re.fullmatch(r"[a-z0-9_]{3,32}", username)
            ):
                raise ValueError
            image_url = self.save_uploaded_image(image_data, "profile") if image_data else None
            cover_image_url = (
                self.save_uploaded_image(cover_image_data, "cover")
                if cover_image_data else None
            )
            if not update_user_profile(
                user["id"], full_name, bio, location, username,
                image_url, cover_image_url,
            ):
                self.send_json({"error": "Profile was not found."}, HTTPStatus.NOT_FOUND)
                return
            updated_profile = find_user_by_id(user["id"])
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json(
                {"error": "Enter a valid name, username, and profile information."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        except IntegrityError as error:
            if error.errno == 1062:
                self.send_json({"error": "That username is already in use."}, HTTPStatus.CONFLICT)
                return
            self.send_json({"error": "Could not save your profile."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"profile": updated_profile})

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
            listing["quantity"] = int(listing["quantity"])
            listing["price"] = str(listing["price"])
            listing["created_at"] = listing["created_at"].isoformat()
        self.send_json({
            "favorite_ids": [int(listing_id) for listing_id in favorite_ids],
            "listings": listings,
        })

    def handle_cart_get(self):
        user = self.current_cart_user()
        if user is None:
            return
        query = parse_qs(urlparse(self.path).query)
        cart_item_ids = None
        if "ids" in query:
            try:
                raw_ids = query["ids"][0].split(",")
                if not raw_ids or len(raw_ids) > 100:
                    raise ValueError
                cart_item_ids = [int(value) for value in raw_ids]
                if any(value < 1 for value in cart_item_ids) or len(set(cart_item_ids)) != len(cart_item_ids):
                    raise ValueError
            except (ValueError, IndexError):
                self.send_json({"error": "Invalid cart selection."}, HTTPStatus.BAD_REQUEST)
                return
        try:
            items = find_cart_items(user["id"], cart_item_ids)
            count = count_cart_items(user["id"])
        except Error:
            logger.exception("Could not load cart for user_id=%s", user["id"])
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        for item in items:
            item["cart_item_id"] = int(item["cart_item_id"])
            item["listing_id"] = int(item["listing_id"]) if item["listing_id"] is not None else None
            item["cart_quantity"] = int(item["cart_quantity"])
            item["available_quantity"] = (
                int(item["available_quantity"]) if item["available_quantity"] is not None else None
            )
            item["price"] = str(item["price"]) if item["price"] is not None else None
            item["is_available"] = bool(
                item["title"]
                and not item["is_hidden"]
                and item["available_quantity"]
                and int(item["seller_id"]) != int(user["id"])
                and item["cart_quantity"] <= item["available_quantity"]
            )
            if not item["title"] or item["is_hidden"]:
                item["availability_message"] = "This item is no longer available."
            elif int(item["seller_id"]) == int(user["id"]):
                item["availability_message"] = "You cannot purchase your own item."
            elif not item["available_quantity"]:
                item["availability_message"] = "This item is out of stock."
            elif item["cart_quantity"] > item["available_quantity"]:
                item["availability_message"] = f"Only {item['available_quantity']} item(s) are currently available."
            else:
                item["availability_message"] = ""
            item["is_hidden"] = bool(item["is_hidden"])
        self.send_json({"items": items, "count": count})

    def handle_cart_count(self):
        user = self.current_cart_user()
        if user is None:
            return
        try:
            count = count_cart_items(user["id"])
        except Error:
            logger.exception("Could not count cart items for user_id=%s", user["id"])
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"count": count})

    def handle_cart_add(self, body):
        user = self.current_cart_user(add=True)
        if user is None:
            return
        try:
            values = json.loads(body)
            if not isinstance(values, dict) or isinstance(values.get("listing_id"), bool):
                raise ValueError
            listing_id = int(values.get("listing_id"))
            quantity = parse_quantity(values.get("quantity", 1), minimum=1)
            replace_quantity = values.get("replace_quantity", False)
            if not isinstance(replace_quantity, bool):
                raise ValueError
            if listing_id < 1:
                raise ValueError
            cart_item_id, error = add_cart_item(
                user["id"], listing_id, quantity, replace_quantity=replace_quantity
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json({"error": "Choose a valid item and quantity."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            logger.exception("Could not add listing_id=%s to cart for user_id=%s", values.get("listing_id"), user["id"])
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if error:
            self.send_json({"error": error}, HTTPStatus.CONFLICT)
            return
        try:
            count = count_cart_items(user["id"])
        except Error:
            logger.exception("Could not refresh cart count for user_id=%s", user["id"])
            self.send_json({"error": "The item was saved, but the cart count could not be refreshed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"cart_item_id": int(cart_item_id), "count": count}, HTTPStatus.CREATED)

    def handle_cart_update(self, cart_item_id, body):
        user = self.current_cart_user()
        if user is None:
            return
        try:
            parsed_id = int(cart_item_id)
            values = json.loads(body)
            if parsed_id < 1 or not isinstance(values, dict) or isinstance(values.get("quantity"), bool):
                raise ValueError
            quantity = parse_quantity(values.get("quantity"), minimum=1)
            updated, error = update_cart_item_quantity(user["id"], parsed_id, quantity)
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json({"error": "Choose a valid cart quantity."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            logger.exception("Could not update cart_item_id=%s for user_id=%s", cart_item_id, user["id"])
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not updated:
            status = HTTPStatus.NOT_FOUND if error == "Cart item not found." else HTTPStatus.CONFLICT
            self.send_json({"error": error}, status)
            return
        try:
            count = count_cart_items(user["id"])
        except Error:
            logger.exception("Could not refresh cart count for user_id=%s", user["id"])
            self.send_json({"error": "Quantity was saved, but the cart count could not be refreshed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"quantity": quantity, "count": count})

    def handle_cart_remove(self, cart_item_id):
        user = self.current_cart_user()
        if user is None:
            return
        try:
            parsed_id = int(cart_item_id)
            if parsed_id < 1:
                raise ValueError
            removed = remove_cart_item(user["id"], parsed_id)
            if not removed:
                self.send_json({"error": "Cart item not found."}, HTTPStatus.NOT_FOUND)
                return
            count = count_cart_items(user["id"])
        except ValueError:
            self.send_json({"error": "Invalid cart item."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            logger.exception("Could not remove cart_item_id=%s for user_id=%s", cart_item_id, user["id"])
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"message": "Item removed from cart.", "count": count})

    def current_cart_user(self, add=False):
        user = self.current_user()
        if user is None:
            message = "Please log in to add items to your cart." if add else "Please log in first."
            self.send_json({"error": message}, HTTPStatus.UNAUTHORIZED)
            return None
        return user

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
            order["quantity"] = int(order["quantity"])
            order["total_price"] = str(order["total_price"])
            order["created_at"] = order["created_at"].isoformat()
            order["can_rate_seller"] = bool(order["can_rate_seller"])
            order["seller_verified"] = bool(order["seller_verified"])
            for field in ("payment_submitted_at", "payment_verified_at"):
                if order[field]:
                    order[field] = order[field].isoformat()
            order["can_cancel"] = bool(order["can_cancel"])
        for order in sales:
            order["id"] = int(order["id"])
            order["listing_id"] = int(order["listing_id"])
            order["buyer_id"] = int(order["buyer_id"])
            order["seller_id"] = int(order["seller_id"])
            order["quantity"] = int(order["quantity"])
            order["total_price"] = str(order["total_price"])
            order["can_seller_cancel"] = bool(order["can_seller_cancel"])
            order["buyer_verified"] = bool(order["buyer_verified"])
            order["created_at"] = order["created_at"].isoformat()
            for field in ("payment_submitted_at", "payment_verified_at"):
                if order[field]:
                    order[field] = order[field].isoformat()
        self.send_json({"viewer_id": int(user["id"]), "orders": orders, "sales": sales})

    def handle_notifications_get(self):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            query = parse_qs(urlparse(self.path).query)
            unread_only = query.get("unread_only", ["0"])[0].lower() in {"1", "true", "yes"}
            limit = int(query.get("limit", ["20"])[0]) if query.get("limit", [""])[0].strip() else 20
            if limit < 1 or limit > 100:
                limit = 20
            notifications = find_notifications_for_user(user["id"], unread_only=unread_only, limit=limit)
        except (TypeError, ValueError, Error):
            self.send_json({"error": "Invalid notification request."}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({
            "notifications": notifications,
            "unread_count": count_unread_notifications(user["id"]),
        })

    def handle_notifications_mark_read(self, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            values = json.loads(body) if body else {}
            if not isinstance(values, dict):
                raise ValueError
            notification_id = values.get("notification_id")
            if notification_id is not None:
                notification_id = int(notification_id)
                if notification_id <= 0:
                    raise ValueError
                mark_notifications_read(user["id"], notification_id)
            else:
                mark_notifications_read(user["id"])
        except (TypeError, ValueError, json.JSONDecodeError):
            self.send_json({"error": "Invalid notification update."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"ok": True})

    def handle_order_payment_post(self, order_id, action):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            parsed_order_id = int(order_id)
            if parsed_order_id <= 0:
                raise ValueError
        except ValueError:
            self.send_json({"error": "Invalid order."}, HTTPStatus.BAD_REQUEST)
            return

        if action == "payment":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json({"error": "Invalid payment submission."}, HTTPStatus.BAD_REQUEST)
                return
            if length <= 0 or length > MAX_PAYMENT_BODY_SIZE:
                self.send_json(
                    {"error": "Payment receipts are limited to 5 MB."},
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            if self.headers.get_content_type() != "multipart/form-data":
                self.send_json({"error": "Submit the payment receipt using the form."}, HTTPStatus.BAD_REQUEST)
                return
            try:
                _, reference_no, payment_amount, receipt_bytes, extension = parse_payment_multipart(
                    self.headers.get("Content-Type", ""),
                    self.rfile.read(length),
                )
            except ValueError as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            try:
                receipt_filename = save_payment_receipt(receipt_bytes, extension)
            except OSError:
                self.send_json(
                    {"error": "The receipt could not be stored securely. Please try again."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            try:
                submitted, error = submit_order_payment(
                    user["id"], parsed_order_id, reference_no, payment_amount, receipt_filename
                )
            except Error:
                remove_payment_receipt(receipt_filename)
                self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if not submitted:
                remove_payment_receipt(receipt_filename)
                self.send_json({"error": error}, HTTPStatus.CONFLICT)
                return
            self.send_json(
                {"message": "Payment submitted. Please wait for the seller to verify your payment."},
                HTTPStatus.CREATED,
            )
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16 * 1024:
                raise ValueError
            values = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(values, dict):
                raise ValueError
            decision = str(values.get("decision", "")).strip().lower()
            reason = str(values.get("rejection_reason", "")).strip()
            if decision not in {"approved", "rejected"}:
                raise ValueError
            if decision == "rejected" and (not reason or len(reason) > 500):
                self.send_json(
                    {"error": "Enter a rejection reason of 1 to 500 characters."},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            reviewed, error = review_order_payment(
                user["id"], parsed_order_id, decision, reason or None
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"error": "Invalid payment review decision."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not reviewed:
            self.send_json({"error": error}, HTTPStatus.CONFLICT)
            return
        notification_warning = False
        try:
            notification = find_order_payment_notification(parsed_order_id)
            if notification and notification.get("buyer_email"):
                notification_warning = not self.send_payment_status_email(notification)
            else:
                notification_warning = True
        except (Error, OSError, ValueError, smtplib.SMTPException):
            notification_warning = True
        response = {"message": "Payment review saved."}
        if notification_warning:
            response["notification_warning"] = (
                "The payment review was saved, but the buyer could not be notified by email."
            )
        self.send_json(response)

    def handle_order_payment_receipt(self, order_id):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            parsed_order_id = int(order_id)
            if parsed_order_id <= 0:
                raise ValueError
            filename = find_order_payment_receipt(user["id"], parsed_order_id)
        except ValueError:
            self.send_json({"error": "Invalid order."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not filename or not re.fullmatch(r"[a-f0-9]{48}\.(?:jpg|png)", filename):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        receipt_path = PRIVATE_PAYMENT_RECEIPT_DIRECTORY / filename
        if not receipt_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            receipt_bytes = receipt_path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg" if filename.endswith(".jpg") else "image/png")
        self.send_header("Content-Length", str(len(receipt_bytes)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", "inline")
        self.end_headers()
        self.wfile.write(receipt_bytes)

    def handle_order_status_update(self, order_id, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            parsed_order_id = int(order_id)
            values = json.loads(body)
            if not isinstance(values, dict):
                raise ValueError
            status = str(values.get("status", "")).strip().lower()
            if parsed_order_id <= 0:
                raise ValueError
            updated, error, changed = update_order_status(user["id"], parsed_order_id, status)
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json({"error": "Invalid order status."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not updated:
            self.send_json({"error": error}, HTTPStatus.CONFLICT)
            return
        if changed:
            try:
                notification = find_order_status_notification(parsed_order_id)
                if notification and notification.get("buyer_email"):
                    self.send_order_status_email(notification)
            except (Error, OSError, ValueError, smtplib.SMTPException):
                pass
        self.send_json({"message": "Order status updated." if changed else "Order status is unchanged."})

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

    def handle_order_create(self, body, payment_info=None):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        receipt_filename = None
        order_id = None
        try:
            values = json.loads(body)
            if not isinstance(values, dict):
                raise ValueError
            listing_id = int(values.get("listing_id"))
            quantity = parse_quantity(values.get("quantity", 1), minimum=1)
            cart_item_id = values.get("cart_item_id")
            if cart_item_id is not None:
                if isinstance(cart_item_id, bool):
                    raise ValueError
                cart_item_id = int(cart_item_id)
                if cart_item_id < 1:
                    raise ValueError
            payment_method = values.get("payment_method")
            if listing_id <= 0:
                raise ValueError
            if not isinstance(payment_method, str) or payment_method not in ORDER_PAYMENT_METHODS:
                self.send_json({"error": "Choose a valid payment method."}, HTTPStatus.BAD_REQUEST)
                return
            requires_payment_evidence = payment_method in {"GCash", "Bank Transfer"}
            payment_reference_no = None
            if requires_payment_evidence:
                if payment_info is None:
                    self.send_json(
                        {"error": "Please complete the required payment information."},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                payment_reference_no = payment_info["reference_no"]
                receipt_filename = save_payment_receipt(
                    payment_info["receipt_bytes"],
                    payment_info["extension"],
                )
            elif payment_info is not None:
                self.send_json(
                    {"error": "Payment information is not required for this payment method."},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            order_id, error, total_price = create_order(
                user["id"],
                listing_id,
                quantity,
                payment_method,
                payment_reference_no,
                receipt_filename,
                cart_item_id,
            )
            if error:
                if receipt_filename:
                    remove_payment_receipt(receipt_filename)
                    receipt_filename = None
                stock_conflict = error.startswith(
                    (
                        "This item is out of stock",
                        "Only ",
                        "The requested quantity",
                        "That payment reference number",
                    )
                )
                status = HTTPStatus.CONFLICT if stock_conflict else HTTPStatus.BAD_REQUEST
                self.send_json({"error": error}, status)
                return
        except (ValueError, TypeError, json.JSONDecodeError):
            if receipt_filename and order_id is None:
                remove_payment_receipt(receipt_filename)
            self.send_json({"error": "Invalid listing."}, HTTPStatus.BAD_REQUEST)
            return
        except OSError:
            if receipt_filename and order_id is None:
                remove_payment_receipt(receipt_filename)
            self.send_json(
                {"error": "The receipt could not be stored securely. Please try again."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        except Error:
            if receipt_filename and order_id is None:
                remove_payment_receipt(receipt_filename)
            logger.exception(
                "Order creation failed for buyer_id=%s listing_id=%s quantity=%s payment_method=%s",
                user["id"],
                listing_id,
                quantity,
                payment_method,
            )
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json(
            {
                "success": True,
                "message": "Order placed successfully.",
                "order_id": int(order_id),
                "id": int(order_id),
                "total_price": str(total_price),
            },
            HTTPStatus.CREATED,
        )

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
                    item["other_verified"] = bool(item["other_verified"])
                    item["created_at"] = item["created_at"].isoformat()
                    item["created_at_ms"] = int(round(float(item["created_at_ms"])))
                self.send_json({
                    "viewer_id": int(user["id"]),
                    "unread_count": int(count_unread_messages(user["id"])),
                    "conversations": conversations,
                })
                return
            other_user_id = int(query.get("other_user_id", [""])[0])
            listing_value = query.get("listing_id", [""])[0]
            try:
                listing_id = int(listing_value) if listing_value else None
            except ValueError:
                listing_id = None
            if other_user_id <= 0 or other_user_id == user["id"]:
                raise ValueError
            other_user = find_public_user_identity(other_user_id)
            messages = find_user_thread(user["id"], other_user_id)
            if other_user is None or not messages:
                self.send_json({"error": "Conversation not found."}, HTTPStatus.NOT_FOUND)
                return
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
            message["created_at_ms"] = int(round(float(message["created_at_ms"])))
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
        self.send_json(
            {
                "viewer_id": int(user["id"]),
                "listing": listing,
                "other_user": {
                    "id": int(other_user["id"]),
                    "full_name": other_user["full_name"],
                    "profile_image_url": other_user["profile_image_url"] or "",
                    "verified": bool(other_user["verified"]),
                } if other_user else None,
                "messages": messages,
            }
        )

    def handle_message_create(self, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        image_url = None
        try:
            values = json.loads(body)
            if not isinstance(values, dict):
                raise ValueError
            listing_id = int(values.get("listing_id"))
            recipient_id = int(values.get("recipient_id"))
            text = str(values.get("body", "") or "").strip()
            image_data = str(values.get("image_data", "") or "")
            reply_to_id = values.get("reply_to_id")
            reply_to_id = int(reply_to_id) if reply_to_id else None
            if (
                listing_id <= 0 or recipient_id <= 0 or recipient_id == user["id"]
                or (not text and not image_data) or len(text) > 2000
            ):
                raise ValueError
            image_url = self.save_message_image(image_data) if image_data else None
            message_id = create_message(
                listing_id,
                user["id"],
                recipient_id,
                text,
                reply_to_id,
                image_url,
            )
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            if image_url:
                self.remove_message_image(image_url)
            self.send_json({"error": "Enter a valid message and image."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            if image_url:
                self.remove_message_image(image_url)
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except OSError:
            if image_url:
                self.remove_message_image(image_url)
            self.send_json({"error": "Could not save the image attachment."}, HTTPStatus.INTERNAL_SERVER_ERROR)
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
            category = normalize_listing_category(values.get("category"))
            item_condition = str(values.get("condition", "")).strip()
            description = str(values.get("description") or "").strip()
            price = float(values.get("price", 0))
            quantity = parse_quantity(values.get("quantity", 1))
            image_data = str(values.get("image_data", "")).strip()
            image_url = self.save_listing_image(image_data)
            if not title or not category or not item_condition or price < 0:
                raise ValueError
            listing_id = create_listing(
                user["id"], title, category, item_condition, description, price, quantity, image_url
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json({"error": "Complete all fields and enter a valid price and quantity."}, HTTPStatus.BAD_REQUEST)
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
            category = normalize_listing_category(values.get("category"))
            item_condition = str(values.get("condition", "")).strip()
            description = str(values.get("description") or "").strip()
            price = float(values.get("price", 0))
            quantity = parse_quantity(values.get("quantity", 1))
            image_data = str(values.get("image_data", "")).strip()
            image_url = self.save_listing_image(image_data) if image_data else None
            if not title or not category or not item_condition or price < 0:
                raise ValueError
            updated = update_listing(
                user["id"], listing_id, title, category, item_condition, description, price, quantity, image_url
            )
            if not updated:
                self.send_json({"error": "Listing was not found."}, HTTPStatus.NOT_FOUND)
                return
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json({"error": "Complete all fields and enter a valid price and quantity."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"id": listing_id})

    @staticmethod
    def save_listing_image(image_data):
        return NCartHandler.save_uploaded_image(image_data, "listing")

    @staticmethod
    def save_uploaded_image(image_data, prefix):
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
        filename = f"{prefix}-{secrets.token_urlsafe(12)}{extension}"
        (upload_directory / filename).write_bytes(image_bytes)
        return f"/static/uploads/{filename}"

    @staticmethod
    def save_message_image(image_data):
        try:
            header, encoded_image = image_data.split(",", 1)
            mime_type = header.removeprefix("data:").removesuffix(";base64")
            image_bytes = base64.b64decode(encoded_image, validate=True)
        except (ValueError, TypeError, base64.binascii.Error):
            raise ValueError("Invalid image attachment.") from None
        if not image_bytes or len(image_bytes) > MAX_MESSAGE_IMAGE_SIZE:
            raise ValueError("Image attachments must be no larger than 5 MB.")
        signatures = {
            "image/jpeg": (".jpg", image_bytes.startswith(b"\xff\xd8\xff")),
            "image/png": (".png", image_bytes.startswith(b"\x89PNG\r\n\x1a\n")),
            "image/webp": (".webp", image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP"),
        }
        extension, valid_signature = signatures.get(mime_type, ("", False))
        if not valid_signature:
            raise ValueError("Upload a valid JPG, PNG, or WEBP image.")
        upload_directory = ROOT / "static" / "uploads"
        upload_directory.mkdir(parents=True, exist_ok=True)
        filename = f"message-{secrets.token_urlsafe(18)}{extension}"
        with (upload_directory / filename).open("xb") as image_file:
            image_file.write(image_bytes)
        return f"/static/uploads/{filename}"

    @staticmethod
    def remove_message_image(image_url):
        upload_directory = (ROOT / "static" / "uploads").resolve()
        image_path = (ROOT / image_url.lstrip("/")).resolve()
        if image_path.parent != upload_directory:
            raise ValueError("Invalid message image path.")
        try:
            image_path.unlink()
        except FileNotFoundError:
            pass

    def send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authenticated_admin(self):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return None
        if not self.is_approved_admin(user):
            self.send_json({"error": "Administrator access is required."}, HTTPStatus.FORBIDDEN)
            return None
        return user

    @staticmethod
    def is_approved_admin(user):
        return bool(
            user
            and user.get("role") == "admin"
            and user.get("status") == "active"
            and user.get("approval_status") == "approved"
            and user.get("account_status") == "active"
        )

    def send_admin_access_denied(self):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
        else:
            self.send_json({"error": "Administrator access is required."}, HTTPStatus.FORBIDDEN)

    def handle_reports_get(self):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            reports = find_reports_for_user(user["id"])
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"reports": reports})

    def handle_report_create(self, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            values = json.loads(body)
            listing_id = int(values.get("listing_id"))
            reason = str(values.get("reason", "")).strip()
            description = str(values.get("description", "")).strip()
            allowed_reasons = {
                "Prohibited item",
                "Misleading information",
                "Inappropriate content",
                "Scam or fraud",
                "Other",
            }
            if listing_id <= 0 or reason not in allowed_reasons or len(description) > 5000:
                raise ValueError
            report_id, error = create_report(user["id"], listing_id, reason, description)
            if error:
                self.send_json({"error": error}, HTTPStatus.BAD_REQUEST)
                return
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            self.send_json({"error": "Enter a valid report reason and listing."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json(
            {
                "id": int(report_id),
                "message": "Report submitted successfully. An administrator will review your report.",
            },
            HTTPStatus.CREATED,
        )

    def handle_admin_report_summary(self):
        user = self.authenticated_admin()
        if user is None:
            return
        try:
            summary = find_report_summary()
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"is_admin": True, "summary": summary})

    def handle_admin_reports_get(self):
        user = self.authenticated_admin()
        if user is None:
            return
        query = parse_qs(urlparse(self.path).query)
        status = query.get("status", [""])[0].strip().lower()
        search = query.get("search", [""])[0].strip()
        report_date = query.get("date", [""])[0].strip()
        reason = query.get("reason", [""])[0].strip()
        allowed_statuses = {"", "pending", "under_review", "approved", "denied"}
        if status not in allowed_statuses or len(search) > 160 or len(reason) > 120:
            self.send_json({"error": "Invalid report filter."}, HTTPStatus.BAD_REQUEST)
            return
        if report_date:
            try:
                time.strptime(report_date, "%Y-%m-%d")
            except ValueError:
                self.send_json({"error": "Invalid report date."}, HTTPStatus.BAD_REQUEST)
                return
        try:
            reports = find_admin_reports(status, search, report_date, reason)
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"reports": reports})

    def handle_admin_report_detail(self, report_id):
        user = self.authenticated_admin()
        if user is None:
            return
        try:
            parsed_id = int(report_id)
            if parsed_id <= 0:
                raise ValueError
            report = find_admin_report_detail(parsed_id)
        except ValueError:
            self.send_json({"error": "Invalid report ID."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if report is None:
            self.send_json({"error": "Report not found."}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"report": report})

    def handle_admin_report_history(self, report_id):
        user = self.authenticated_admin()
        if user is None:
            return
        try:
            parsed_id = int(report_id)
            if parsed_id <= 0:
                raise ValueError
            report = find_admin_report_detail(parsed_id)
        except ValueError:
            self.send_json({"error": "Invalid report ID."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if report is None:
            self.send_json({"error": "Report not found."}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"history": report["history"]})

    def handle_admin_report_review(self, report_id, body):
        user = self.authenticated_admin()
        if user is None:
            return
        try:
            parsed_id = int(report_id)
            values = json.loads(body)
            status = str(values.get("status", "")).strip().lower()
            note = str(values.get("note", "")).strip()
            action = str(values.get("action", "none")).strip().lower()
            allowed_statuses = {"under_review", "approved", "denied"}
            allowed_actions = {"none", "flag_listing", "hide_listing", "suspend_user"}
            if parsed_id <= 0 or status not in allowed_statuses or action not in allowed_actions:
                raise ValueError
            if status in {"approved", "denied"} and not note:
                self.send_json({"error": "Enter an admin note before resolving this report."}, HTTPStatus.BAD_REQUEST)
                return
            if len(note) > 5000 or (status != "approved" and action != "none"):
                raise ValueError
            updated, error = review_report(parsed_id, user["id"], status, note, action)
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            self.send_json({"error": "Enter a valid report decision."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not updated:
            self.send_json({"error": error}, HTTPStatus.CONFLICT)
            return
        self.send_json({"message": "Report review saved."})

    def handle_pending_accounts_get(self):
        if self.authenticated_admin() is None:
            return
        try:
            accounts = find_pending_accounts()
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json(
            {
                "accounts": [
                    {
                        **account,
                        "id": int(account["id"]),
                    }
                    for account in accounts
                ]
            }
        )

    def handle_pending_account_review(self, user_id, values):
        admin = self.authenticated_admin()
        if admin is None:
            return
        try:
            parsed_id = int(user_id)
            decision = str(values.get("decision", "")).strip().lower()
            if parsed_id <= 0 or decision not in {"approved", "confirmed", "rejected"}:
                raise ValueError
            result = review_pending_account(parsed_id, int(admin["id"]), decision)
        except (ValueError, TypeError, AttributeError):
            self.send_json(
                {"error": "Choose Confirm or Reject for a valid account."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not result:
            self.send_json(
                {"error": "This account is no longer awaiting confirmation."},
                HTTPStatus.CONFLICT,
            )
            return
        if result == "admin_approved":
            delivery_state, retry_after = self.send_approved_admin_registration_otp(
                parsed_id,
                int(admin["id"]),
                "not_sent",
            )
            if delivery_state != "sent":
                if delivery_state == "cooldown":
                    self.send_json(
                        {
                            "error": "The Admin registration was approved, but email delivery is cooling down. Retry after the displayed delay.",
                            "retry_after": retry_after,
                        },
                        HTTPStatus.TOO_MANY_REQUESTS,
                    )
                else:
                    self.send_json(
                        {
                            "error": (
                                "The Admin registration was approved, but the verification email "
                                "could not be sent. Retry sending the OTP from Pending Accounts."
                            )
                        },
                        HTTPStatus.SERVICE_UNAVAILABLE
                        if delivery_state == "delivery_failed"
                        else HTTPStatus.CONFLICT,
                    )
                return
            self.send_json(
                {
                    "message": (
                        "Admin registration approved. The email verification code was sent."
                    )
                }
            )
            return
        self.send_json(
            {
                "message": (
                    "Account confirmed."
                    if result == "student_approved"
                    else "Account rejected."
                )
            }
        )

    def handle_admin_registration_otp_retry(self, user_id):
        admin = self.authenticated_admin()
        if admin is None:
            return
        try:
            parsed_id = int(user_id)
            if parsed_id <= 0:
                raise ValueError
            state, retry_after = self.send_approved_admin_registration_otp(
                parsed_id,
                int(admin["id"]),
                "failed",
            )
            if state == "not_retryable":
                state, retry_after = self.send_approved_admin_registration_otp(
                    parsed_id,
                    int(admin["id"]),
                    "not_sent",
                )
        except (ValueError, TypeError, AttributeError):
            self.send_json({"error": "Choose a valid Admin registration."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if state == "sent":
            self.send_json({"message": "The Admin verification email was sent."})
            return
        if state == "cooldown":
            self.send_json(
                {"error": "Please wait before retrying the verification email.", "retry_after": retry_after},
                HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        if state == "delivery_failed":
            self.send_json(
                {"error": "Could not send the verification email. Check SMTP configuration and retry later."},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if state == "rate_limited":
            self.send_json(
                {"error": "Too many verification emails were requested. Try again in an hour."},
                HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        self.send_json(
            {"error": "This Admin registration is no longer eligible for an OTP retry."},
            HTTPStatus.CONFLICT,
        )

    def handle_admin_student_verifications_get(self):
        if self.authenticated_admin() is None:
            return
        try:
            verifications = find_pending_student_verifications()
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json(
            {
                "verifications": [
                    {
                        **verification,
                        "id": int(verification["id"]),
                    }
                    for verification in verifications
                ]
            }
        )

    def handle_admin_student_verification_image(self, user_id):
        if self.authenticated_admin() is None:
            return
        try:
            parsed_id = int(user_id)
            if parsed_id <= 0:
                raise ValueError
            relative_path = find_student_verification_image(parsed_id)
        except ValueError:
            self.send_json({"error": "Invalid student verification ID."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not relative_path:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        path = (ROOT / relative_path).resolve()
        private_directory = PRIVATE_STUDENT_ID_DIRECTORY.resolve()
        content_types = {
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        content_type = content_types.get(path.suffix.lower())
        if not content_type or not path.is_relative_to(private_directory):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            image = path.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        except OSError:
            self.send_json({"error": "Could not read the submitted Student ID image."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(image)))
        self.send_header("Content-Disposition", "inline; filename=\"student-id\"")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(image)

    def handle_admin_student_verification_review(self, user_id, body):
        admin = self.authenticated_admin()
        if admin is None:
            return
        try:
            parsed_id = int(user_id)
            values = json.loads(body)
            status = str(values.get("status", "")).strip().lower()
            rejection_reason = str(values.get("reason", "")).strip()
            if parsed_id <= 0 or status not in {"verified", "rejected"}:
                raise ValueError
            if status == "rejected" and not rejection_reason:
                self.send_json(
                    {"error": "Explain why this Student ID could not be verified."},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if len(rejection_reason) > 500:
                raise ValueError
            reviewed = review_student_verification(
                parsed_id,
                admin["id"],
                status,
                rejection_reason if status == "rejected" else "",
            )
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            self.send_json({"error": "Enter a valid verification decision."}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not reviewed:
            self.send_json(
                {"error": "This student is no longer awaiting verification."},
                HTTPStatus.CONFLICT,
            )
            return
        self.send_json({"message": "Student verification decision saved."})

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
    def send_admin_registration_otp_email(email, otp, account_type="student"):
        username = os.getenv("NCART_SMTP_USERNAME", "")
        password = os.getenv("NCART_SMTP_PASSWORD", "").replace(" ", "")
        if not username or not password:
            raise OSError("SMTP credentials are not configured")
        message = EmailMessage()
        is_admin = account_type == "admin"
        message["Subject"] = (
            "Your NCart Admin registration was approved"
            if is_admin
            else "Verify your NCart account registration"
        )
        message["From"] = os.getenv("NCART_SMTP_FROM", username)
        message["To"] = email
        if is_admin:
            verification_url = (
                os.getenv("NCART_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
                + "/templates/create-account.html?"
                + urlencode({"admin_verification": "1", "email": email})
            )
            message.set_content(
                "NCart Admin Registration\n\n"
                "Your Admin account registration has been approved. Enter this "
                "verification code on the NCart verification page to activate "
                "your Admin account:\n\n"
                f"{otp}\n\n"
                "This code expires in five minutes. Do not share this code with "
                "anyone, including someone claiming to be an NCart administrator.\n\n"
                f"Verification page: {verification_url}\n"
            )
        else:
            message.set_content(
                "Use this one-time code to verify the email address for your NCart "
                "account registration:\n\n"
                f"{otp}\n\n"
                "This code expires in five minutes. If you did not request this, "
                "you can ignore this email."
            )
        with smtplib.SMTP(
            os.getenv("NCART_SMTP_HOST", "smtp.gmail.com"),
            int(os.getenv("NCART_SMTP_PORT", "587")),
            timeout=10,
        ) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)

    @staticmethod
    def hash_admin_registration_otp(email, otp):
        message = f"ncart-admin-registration:{email}:{otp}".encode("utf-8")
        return hmac.new(
            SESSION_SECRET.encode("utf-8"),
            message,
            hashlib.sha256,
        ).hexdigest()

    def send_approved_admin_registration_otp(self, user_id, admin_id, expected_status):
        applicant = claim_admin_registration_otp_delivery(
            user_id,
            admin_id,
            expected_status,
        )
        if applicant is None:
            return "not_retryable", 0
        email = applicant["email"]
        otp = f"{secrets.randbelow(1_000_000):06d}"
        now = int(time.time())
        otp_hash = self.hash_admin_registration_otp(email, otp)
        state, retry_after = issue_admin_registration_otp(
            email,
            otp_hash,
            now,
            now + ADMIN_REGISTRATION_OTP_TTL,
            applicant["full_name"],
            applicant["password_hash"],
            "admin",
            first_name=applicant["first_name"],
            middle_name=applicant["middle_name"],
            last_name=applicant["last_name"],
        )
        if state != "sent":
            update_admin_otp_delivery_status(email, "failed")
            return state, retry_after
        try:
            self.send_admin_registration_otp_email(email, otp, "admin")
        except (OSError, smtplib.SMTPException, ValueError):
            invalidate_admin_registration_otp(email, otp_hash)
            update_admin_otp_delivery_status(email, "failed")
            return "delivery_failed", 0
        update_admin_otp_delivery_status(email, "sent")
        return "sent", 0

    def handle_admin_registration_send_otp(
        self, values, student_id_image=None, uploaded_content_type=""
    ):
        account_type = values.get("account_type", [""])[0].strip().lower()
        if account_type not in {"student", "admin"}:
            self.send_json({"error": "Select Student or Admin as your account type."}, HTTPStatus.BAD_REQUEST)
            return
        first_name = normalize_name_part(values.get("first_name", [""])[0])
        middle_name = normalize_name_part(values.get("middle_name", [""])[0])
        last_name = normalize_name_part(values.get("last_name", [""])[0])
        full_name = compose_full_name(first_name, middle_name, last_name)
        email = values.get("email", [""])[0].strip().lower()
        student_number = (
            values.get("student_number", [""])[0].strip()
            if account_type == "student"
            else ""
        )
        password = values.get("password", [""])[0]
        confirmation = values.get("confirm_password", [""])[0]
        google_token = values.get("google_token", [""])[0]
        google_registration = GOOGLE_REGISTRATIONS.get(google_token)
        if google_token:
            if not google_registration or google_registration["expires_at"] < time.time():
                self.send_json({"error": "Your Google registration expired. Please sign in with Google again."}, HTTPStatus.BAD_REQUEST)
                return
            email = google_registration["email"]

        if not first_name or not last_name:
            self.send_json({"error": "Enter both your first name and last name."}, HTTPStatus.BAD_REQUEST)
            return
        if len(first_name) > 120 or len(middle_name) > 120 or len(last_name) > 120 or len(full_name) > 120:
            self.send_json({"error": "Your name must be 120 characters or fewer."}, HTTPStatus.BAD_REQUEST)
            return
        if len(email) > 255 or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is None:
            self.send_json({"error": "Enter a valid email address."}, HTTPStatus.BAD_REQUEST)
            return
        if account_type == "student" and re.fullmatch(r"\d{4}-\d{4}", student_number, re.ASCII) is None:
            self.send_json(
                {"error": "Student Number must follow the format 0000-0000."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        strong_password = (
            len(password) >= 8
            and re.search(r"[a-z]", password)
            and re.search(r"[A-Z]", password)
            and re.search(r"\d", password)
            and re.search(r"[^A-Za-z\d]", password)
        )
        if not strong_password:
            self.send_json(
                {"error": "Password must have at least 8 characters, one uppercase letter, one lowercase letter, one number, and one special character."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if password != confirmation:
            self.send_json({"error": "Passwords do not match."}, HTTPStatus.BAD_REQUEST)
            return
        image_path = None
        try:
            existing_user = find_user_by_email(email)
            if existing_user is not None:
                is_student_resubmission = (
                    account_type == "student"
                    and existing_user.get("role") in {"buyer", "seller", "student", "user"}
                    and existing_user.get("account_status") == "rejected"
                    and existing_user.get("status") != "pending"
                )
                if not is_student_resubmission:
                    self.send_json(
                        {"error": "Registration could not be completed. Check the details or log in if you already have an account."},
                        HTTPStatus.CONFLICT,
                    )
                    return
                if not verify_password(password, existing_user["password_hash"]):
                    self.send_json(
                        {"error": "Log in with the existing account password to continue."},
                        HTTPStatus.CONFLICT,
                    )
                    return
            if account_type == "admin":
                if student_id_image:
                    self.send_json(
                        {"error": "Student ID images are only accepted for Student accounts."},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                created_user_id = create_pending_admin_registration(
                    full_name,
                    first_name,
                    middle_name or None,
                    last_name,
                    email,
                    hash_password(password),
                )
                if created_user_id is None:
                    self.send_json(
                        {"error": "Registration could not be completed. Check the details or log in if you already have an account."},
                        HTTPStatus.CONFLICT,
                    )
                    return
                self.send_json(
                    {
                        "message": (
                            "Your Admin account registration has been submitted successfully. "
                            "Please wait for administrator approval. An email verification code "
                            "will be sent to your registered email after your registration is approved."
                        )
                    },
                    HTTPStatus.ACCEPTED,
                )
                return
            if account_type == "student":
                matching_number = find_user_by_student_number(student_number)
                if matching_number is not None and (
                    existing_user is None or int(matching_number["id"]) != int(existing_user["id"])
                ):
                    self.send_json({"error": "That Student Number is already registered."}, HTTPStatus.CONFLICT)
                    return
            if account_type == "student":
                if student_id_image:
                    image_content_type, image_extension = identify_student_id_image(student_id_image)
                    if uploaded_content_type not in {image_content_type, "application/octet-stream"}:
                        self.send_json(
                            {"error": "The uploaded image format does not match its contents."},
                            HTTPStatus.BAD_REQUEST,
                        )
                        return
                    image_path = save_student_id_image(student_id_image, image_extension)
            elif student_id_image:
                self.send_json(
                    {"error": "Student ID images are only required for Student accounts."},
                    HTTPStatus.BAD_REQUEST,
                )
                return

            otp = f"{secrets.randbelow(1_000_000):06d}"
            now = int(time.time())
            otp_hash = self.hash_admin_registration_otp(email, otp)
            state, retry_after = issue_admin_registration_otp(
                email,
                otp_hash,
                now,
                now + ADMIN_REGISTRATION_OTP_TTL,
                full_name,
                hash_password(password),
                account_type,
                student_number if account_type == "student" else None,
                image_path,
                None,
                first_name=first_name,
                middle_name=middle_name or None,
                last_name=last_name,
            )
            if state == "cooldown":
                if image_path:
                    remove_student_id_image(image_path)
                self.send_json(
                    {"error": "Please wait before requesting another code.", "retry_after": retry_after},
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
                return
            if state == "rate_limited":
                if image_path:
                    remove_student_id_image(image_path)
                self.send_json(
                    {"error": "Too many verification codes were requested. Try again in an hour."},
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
                return
            if state != "sent":
                if image_path:
                    remove_student_id_image(image_path)
                self.send_json({"error": "Could not start email verification. Please try again."}, HTTPStatus.CONFLICT)
                return
            try:
                self.send_admin_registration_otp_email(email, otp, "student")
            except (OSError, smtplib.SMTPException):
                invalidate_admin_registration_otp(email, otp_hash)
                if image_path:
                    remove_student_id_image(image_path)
                raise
        except IntegrityError:
            if image_path:
                remove_student_id_image(image_path)
            self.send_json(
                {"error": "Registration could not be completed. Check the details or log in if you already have an account."},
                HTTPStatus.CONFLICT,
            )
            return
        except (OSError, smtplib.SMTPException):
            if image_path:
                remove_student_id_image(image_path)
            self.send_json(
                {"error": "Could not send the verification email. Check the email address or try again later."},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        except ValueError as error:
            if image_path:
                remove_student_id_image(image_path)
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        except Error:
            if image_path:
                remove_student_id_image(image_path)
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.send_json(
            {
                "message": "A verification code was sent to your email. It expires in five minutes.",
                "retry_after": ADMIN_REGISTRATION_OTP_COOLDOWN,
            },
            HTTPStatus.ACCEPTED,
        )

    def handle_admin_registration_resend_otp(self, values):
        email = str(values.get("email", "")).strip().lower()
        if not email or len(email) > 255 or "@" not in email:
            self.send_json({"error": "Enter the email address used for this registration."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            existing_user = find_user_by_email(email)
            admin_registration = bool(
                existing_user
                and existing_user.get("role") != "admin"
                and existing_user.get("admin_request_status") == "approved"
                and existing_user.get("status") == "pending"
            )
            student_resubmission = bool(
                existing_user
                and existing_user.get("role") in {"buyer", "seller", "student", "user"}
                and existing_user.get("status") != "pending"
                and existing_user.get("account_status") == "rejected"
            )
            if existing_user is not None and not admin_registration and not student_resubmission:
                self.send_json(
                    {"error": "Registration could not be completed. Check the details or log in if you already have an account."},
                    HTTPStatus.CONFLICT,
                )
                return
            otp = f"{secrets.randbelow(1_000_000):06d}"
            now = int(time.time())
            otp_hash = self.hash_admin_registration_otp(email, otp)
            state, retry_after = issue_admin_registration_otp(
                email,
                otp_hash,
                now,
                now + ADMIN_REGISTRATION_OTP_TTL,
            )
            if state == "cooldown":
                self.send_json(
                    {"error": "Please wait before requesting another code.", "retry_after": retry_after},
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
                return
            if state == "rate_limited":
                self.send_json(
                    {"error": "Too many verification codes were requested. Try again in an hour."},
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
                return
            if state != "sent":
                if state == "not_approved":
                    self.send_json(
                        {"error": "Admin registration must be approved before an email verification code can be sent."},
                        HTTPStatus.FORBIDDEN,
                    )
                    return
                self.send_json({"error": "This verification request expired. Please submit the registration form again."}, HTTPStatus.GONE)
                return
            try:
                self.send_admin_registration_otp_email(
                    email,
                    otp,
                    "admin" if admin_registration else "student",
                )
            except (OSError, smtplib.SMTPException, ValueError):
                invalidate_admin_registration_otp(email, otp_hash)
                if admin_registration:
                    update_admin_otp_delivery_status(
                        email,
                        "failed",
                        existing_user["admin_otp_delivery_status"],
                    )
                raise
            if admin_registration:
                update_admin_otp_delivery_status(
                    email,
                    "sent",
                    existing_user["admin_otp_delivery_status"],
                )
        except (OSError, smtplib.SMTPException, ValueError):
            self.send_json(
                {"error": "Could not resend the verification email. Try again later."},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json(
            {
                "message": "A new verification code was sent. It expires in five minutes.",
                "retry_after": ADMIN_REGISTRATION_OTP_COOLDOWN,
            },
            HTTPStatus.ACCEPTED,
        )

    def handle_admin_registration_verify_otp(self, values):
        email = str(values.get("email", "")).strip().lower()
        otp = str(values.get("otp", "")).strip()
        if not email or len(email) > 255 or "@" not in email or not re.fullmatch(r"\d{6}", otp):
            self.send_json({"error": "Enter the six-digit verification code sent to your email."}, HTTPStatus.BAD_REQUEST)
            return
        otp_hash = self.hash_admin_registration_otp(email, otp)
        try:
            state, user_id = complete_admin_registration(email, otp_hash, int(time.time()))
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if state == "invalid":
            self.send_json({"error": "Invalid OTP. Please try again.", "attempts_remaining": 4}, HTTPStatus.BAD_REQUEST)
            return
        if state == "locked":
            self.send_json(
                {"error": "Too many incorrect OTP attempts. Request a new code after the resend cooldown."},
                HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        if state == "expired":
            self.send_json({"error": "OTP expired. Request a new code and try again."}, HTTPStatus.GONE)
            return
        if state == "not_approved":
            self.send_json(
                {"error": "Admin registration must be approved before OTP verification."},
                HTTPStatus.FORBIDDEN,
            )
            return
        if state in {"email_exists", "registration_conflict"}:
            self.send_json(
                {"error": "Registration could not be completed. Check the details or log in if you already have an account."},
                HTTPStatus.CONFLICT,
            )
            return
        if state not in {"verified", "verified_admin"} or user_id is None:
            self.send_json({"error": "No active verification was found. Submit the registration form again."}, HTTPStatus.GONE)
            return

        for registration_token, registration in list(GOOGLE_REGISTRATIONS.items()):
            if registration["email"] == email:
                GOOGLE_REGISTRATIONS.pop(registration_token, None)
        if state == "verified_admin":
            success_message = "Your Admin account has been verified and activated. You can now log in."
            self.send_json(
                {
                    "message": success_message,
                    "redirect": "/templates/login.html?"
                    + urlencode({"message": success_message, "login_success": "1"}),
                }
            )
            return
        pending_message = (
            "Your account has been successfully verified. Please wait for administrator approval before accessing NCart."
        )
        self.send_response(HTTPStatus.OK)
        payload = json.dumps(
            {
                "message": pending_message,
                "redirect": "/pending-account?" + urlencode(
                    {"state": "pending"}
                ),
            }
        ).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def send_order_status_email(order):
        username = os.getenv("NCART_SMTP_USERNAME", "")
        password = os.getenv("NCART_SMTP_PASSWORD", "").replace(" ", "")
        if not username or not password:
            return
        status = {
            "order_placed": "Order Placed",
            "preparing_order": "Preparing Order",
            "ready_for_pickup": "Ready for Pick Up",
            "completed": "Completed",
            "cancelled": "Cancelled",
        }.get(str(order.get("status", "")), "Order Updated")
        message = EmailMessage()
        message["Subject"] = f"NCart order update: {status}"
        message["From"] = os.getenv("NCART_SMTP_FROM", username)
        message["To"] = order["buyer_email"]
        message.set_content(
            f"Hi {order.get('buyer_name', 'there')},\n\n"
            f"Your NCart order #{order['id']} for {order['listing_title']} is now {status}.\n\n"
            "You can open My Orders in NCart to track the latest order progress.\n\n"
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

    @staticmethod
    def send_payment_status_email(order):
        username = os.getenv("NCART_SMTP_USERNAME", "")
        password = os.getenv("NCART_SMTP_PASSWORD", "").replace(" ", "")
        if not username or not password:
            return False
        verified = order.get("payment_status") == "verified"
        result = "successfully verified" if verified else "rejected"
        message = EmailMessage()
        message["Subject"] = f"NCart payment {result}: order #{order['id']}"
        message["From"] = os.getenv("NCART_SMTP_FROM", username)
        message["To"] = order["buyer_email"]
        content = (
            f"Your payment for order #{order['id']} ({order['listing_title']}) "
            f"was {result}."
        )
        if not verified and order.get("payment_rejection_reason"):
            content += f"\n\nReason: {order['payment_rejection_reason']}"
        content += "\n\nOpen My Orders in NCart to view the latest payment status."
        message.set_content(f"Hi {order.get('buyer_name', 'there')},\n\n{content}\n\nThank you,\nNCart")
        with smtplib.SMTP(
            os.getenv("NCART_SMTP_HOST", "smtp.gmail.com"),
            int(os.getenv("NCART_SMTP_PORT", "587")),
            timeout=10,
        ) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
        return True

    def handle_login(self, values):
        submitted_email = values.get("email", [""])[0].strip()
        email = submitted_email.lower()
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
            self.redirect_with_message(
                "/templates/login.html",
                "Invalid email or password.",
                "error",
                extra={"email": submitted_email},
            )
            return
        if user.get("role") != "admin" and user.get("admin_request_status") == "pending":
            self.redirect_with_message(
                "/pending-account",
                "Your Admin account registration is awaiting administrator approval.",
                "message",
                {"state": "admin_pending"},
            )
            return
        if user.get("role") != "admin" and user.get("admin_request_status") == "approved":
            self.redirect_with_message(
                "/templates/create-account.html",
                "Your Admin registration has been approved. Enter the email verification code to activate your account.",
                "message",
                {"admin_verification": "1", "email": email},
            )
            return
        if user.get("role") != "admin" and user.get("admin_request_status") == "rejected":
            self.redirect_with_message(
                "/pending-account",
                "Your Admin registration has not been approved.",
                "error",
                {"state": "rejected"},
            )
            return
        if user.get("role") != "admin" and user.get("status") == "pending":
            self.redirect_with_message(
                "/pending-account",
                "Your account has been successfully verified. Please wait for administrator approval before accessing NCart.",
                "message",
                {"state": "pending"},
            )
            return
        if user.get("role") != "admin" and user.get("status") == "rejected":
            self.redirect_with_message(
                "/pending-account",
                "Your account has not been approved by the administrator.",
                "error",
                {"state": "rejected"},
            )
            return
        if user.get("role") == "admin" and (
            user.get("status") != "active"
            or user.get("account_status") not in {"active", "verified"}
            or user.get("approval_status") != "approved"
        ):
            self.redirect_with_message(
                "/templates/login.html",
                "This Admin account is not active. Contact the NCart system owner.",
                "error",
            )
            return
        if user.get("account_status") == "pending_verification":
            self.redirect_with_message(
                "/templates/login.html",
                "Your account is pending Student ID verification. You can log in after an administrator approves it.",
                "message",
            )
            return
        if user.get("account_status") == "rejected":
            rejection_reason = user.get("verification_rejection_reason")
            message = "Your Student ID could not be verified."
            if rejection_reason:
                message += f" {rejection_reason}"
            message += " Please submit another valid ID."
            self.redirect_with_message(
                "/templates/create-account.html",
                message,
                "error",
                {"email": submitted_email, "retry_verification": "1"},
            )
            return
        if user.get("account_status") not in {"active", "verified"}:
            self.redirect_with_message(
                "/templates/login.html",
                "This account is suspended. Contact NCart support.",
                "error",
            )
            return
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = {"id": int(user["id"]), "email": user["email"]}
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/templates/login.html?message=Login+successful.+Welcome+back.&login_success=1")
        self.send_header("Set-Cookie", f"ncart_session={make_session_cookie(user['id'])}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()

    def current_account(self):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookies.get("ncart_session")
        if not token:
            return None
        session = SESSIONS.get(token.value)
        if session:
            try:
                return find_user_by_id(session["id"])
            except Error:
                return None
        session = read_signed_session(token.value)
        if not session:
            return None
        try:
            return find_user_by_id(session["id"])
        except Error:
            return None

    def current_user(self):
        user = self.current_account()
        return (
            user
            if user
            and user.get("status") == "active"
            and user.get("account_status") in {"active", "verified"}
            and user.get("approval_status") == "approved"
            else None
        )

    def handle_logout(self):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookies.get("ncart_session")
        if token:
            SESSIONS.pop(token.value, None)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", "ncart_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        self.end_headers()

    def handle_register(self, values, student_id_image=None, uploaded_content_type=""):
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

        first_name = normalize_name_part(values.get("first_name", [""])[0])
        middle_name = normalize_name_part(values.get("middle_name", [""])[0])
        last_name = normalize_name_part(values.get("last_name", [""])[0])
        full_name = compose_full_name(first_name, middle_name, last_name)
        email = values.get("email", [""])[0].strip().lower()
        password = values.get("password", [""])[0]
        confirmation = values.get("confirm_password", [""])[0]
        account_type = values.get("account_type", [""])[0].strip().lower()
        if account_type not in {"student", "admin"}:
            self.redirect_with_message(
                "/templates/create-account.html",
                "Select Student or Admin as your account type.",
                "error",
            )
            return
        if account_type == "admin":
            self.redirect_with_message(
                "/templates/create-account.html",
                "Admin registration must be submitted for administrator approval through the registration form.",
                "error",
            )
            return
        student_number = values.get("student_number", [""])[0].strip()
        student_id_number = student_number

        if google_registration:
            email = google_registration["email"]

        strong_password = (
            len(password) >= 8
            and re.search(r"[a-z]", password)
            and re.search(r"[A-Z]", password)
            and re.search(r"\d", password)
            and re.search(r"[^A-Za-z\d]", password)
        )
        if not first_name or not last_name:
            self.redirect_with_message(
                "/templates/create-account.html",
                "Enter both your first name and last name.",
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
        if account_type == "student" and re.fullmatch(
            r"\d{4}-\d{4}", student_number, re.ASCII
        ) is None:
            self.redirect_with_message(
                "/templates/create-account.html",
                "Student Number must follow the format 0000-0000.",
                "error",
            )
            return
        if (
            len(first_name) > 120
            or len(middle_name) > 120
            or len(last_name) > 120
            or len(full_name) > 120
            or len(email) > 255
        ):
            self.redirect_with_message(
                "/templates/create-account.html",
                "Check that your name and email address are within the allowed length.",
                "error",
            )
            return

        try:
            image_path = None
            if account_type == "student":
                if not student_id_image:
                    raise ValueError("Upload a clear image of your valid Student ID.")
                image_content_type, image_extension = identify_student_id_image(student_id_image)
                if uploaded_content_type not in {image_content_type, "application/octet-stream"}:
                    raise ValueError("The uploaded image format does not match its contents.")
            elif student_id_image:
                raise ValueError("Student ID images should only be submitted for Student accounts.")
            existing_user = find_user_by_email(email)
            if existing_user is not None:
                if (
                    existing_user.get("account_status") == "rejected"
                    and account_type == "student"
                ):
                    if not verify_password(password, existing_user["password_hash"]):
                        self.redirect_with_message(
                            "/templates/login.html",
                            "An account with that email already exists. Log in to continue.",
                            "error",
                            {"email": email},
                        )
                        return
                    image_path = save_student_id_image(student_id_image, image_extension)
                    try:
                        updated, old_image_path = resubmit_student_verification(
                            existing_user["id"],
                            student_id_number,
                            image_path,
                        )
                    except Error:
                        remove_student_id_image(image_path)
                        raise
                    if not updated:
                        remove_student_id_image(image_path)
                        self.redirect_with_message(
                            "/templates/login.html",
                            "Your verification status changed. Log in to check your account.",
                            "error",
                        )
                        return
                    try:
                        remove_student_id_image(old_image_path)
                    except OSError:
                        self.redirect_with_message(
                            "/templates/login.html",
                            "Your new Student ID was submitted and is pending verification, but an older private upload could not be removed. Contact support.",
                            "error",
                        )
                        return
                    self.redirect_with_message(
                        "/templates/login.html",
                        "Your new Student ID was submitted. Your account is pending administrator verification.",
                        "message",
                    )
                    return
                if existing_user.get("account_status") == "pending_verification":
                    self.redirect_with_message(
                        "/templates/login.html",
                        "Your Student ID is pending administrator verification.",
                        "message",
                    )
                    return
                self.redirect_with_message(
                    "/templates/create-account.html",
                    "An account with that email already exists.",
                    "error",
                )
                return
            if account_type == "student":
                image_path = save_student_id_image(student_id_image, image_extension)
            try:
                create_user(
                    full_name,
                    email,
                    hash_password(password),
                    student_id_number,
                    image_path,
                    student_number,
                    first_name=first_name,
                    middle_name=middle_name or None,
                    last_name=last_name,
                )
            except Error:
                if image_path:
                    remove_student_id_image(image_path)
                raise
        except IntegrityError:
            self.redirect_with_message("/templates/create-account.html", "An account with that email already exists.", "error")
            return
        except ValueError as error:
            self.redirect_with_message("/templates/create-account.html", str(error), "error")
            return
        except Error:
            self.redirect_with_message("/templates/create-account.html", "Database connection failed.", "error")
            return
        except OSError:
            self.redirect_with_message(
                "/templates/create-account.html",
                "The Student ID image could not be stored securely. Please try again.",
                "error",
            )
            return

        if google_registration:
            GOOGLE_REGISTRATIONS.pop(google_token, None)
        self.redirect_with_message(
            "/templates/login.html",
            "Account created. Your Student ID is pending administrator verification. You can log in after it is approved.",
            "message",
        )

    def redirect_with_message(self, path, message, kind, extra=None, session_user_id=None):
        parameters = {kind: message}
        if extra:
            parameters.update(extra)
        query = urlencode(parameters)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"{path}?{query}")
        if session_user_id is not None:
            self.send_header(
                "Set-Cookie",
                f"ncart_session={make_session_cookie(session_user_id)}; Path=/; HttpOnly; SameSite=Lax",
            )
        self.end_headers()

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")



if __name__ == "__main__":
    initialize_database()

    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 8000))

    server = ThreadingHTTPServer((host, port), NCartHandler)
    print(f"NCart is running at http://{host}:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping NCart")
    finally:
        server.server_close()

