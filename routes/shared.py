import base64
import hashlib
import hmac
import json
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
    ORDER_PAYMENT_METHODS,
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
    find_favorite_ids,
    find_favorite_listings,
    find_user_by_email,
    find_user_by_id,
    initialize_database,
    update_user_password,
    update_user_profile,
    update_listing,
    update_order_status,
    remove_favorite,
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

ROOT = Path(__file__).resolve().parent.parent
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
