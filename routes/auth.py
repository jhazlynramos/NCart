from flask import Blueprint

from .route_helpers import register_legacy_routes
from .shared import *


auth_bp = Blueprint("auth", __name__)
register_legacy_routes(
    auth_bp,
    [
        ("/", ["GET"]),
        ("/login", ["GET", "POST"]),
        ("/create-account", ["GET"]),
        ("/register", ["POST"]),
        ("/logout", ["GET"]),
        ("/reset-password", ["GET", "POST"]),
        ("/request-reset", ["POST"]),
        ("/auth/google", ["GET"]),
        ("/auth/google/callback", ["GET"]),
        ("/api/admin-registration/send-otp", ["POST"]),
        ("/api/admin-registration/resend-otp", ["POST"]),
        ("/api/admin-registration/verify-otp", ["POST"]),
        ("/home", ["GET"]),
        ("/dashboard", ["GET"]),
        ("/templates/<path:filename>", ["GET"]),
    ],
)


class AuthRoutesMixin:
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
                and existing_user.get("account_status") == "rejected"
                and existing_user.get("status") != "pending"
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
        if state in {"email_exists", "registration_conflict"}:
            self.send_json(
                {"error": "Registration could not be completed. Check the details or log in if you already have an account."},
                HTTPStatus.CONFLICT,
            )
            return
        if state == "not_approved":
            self.send_json(
                {"error": "Admin registration must be approved before OTP verification."},
                HTTPStatus.FORBIDDEN,
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
            self.send_response(HTTPStatus.OK)
            payload = json.dumps(
                {
                    "message": success_message,
                    "redirect": "/templates/login.html?"
                    + urlencode({"message": success_message, "login_success": "1"}),
                }
            ).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
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
