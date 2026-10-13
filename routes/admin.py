from flask import Blueprint

from .route_helpers import register_legacy_routes
from .shared import *


admin_bp = Blueprint("admin", __name__)
register_legacy_routes(
    admin_bp,
    [
        ("/admin", ["GET"]),
        ("/templates/admin.html", ["GET"]),
        ("/api/admin/<path:admin_route>", ["GET", "POST", "PUT", "DELETE"]),
    ],
)


class AdminRoutesMixin:
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
            self.send_json({"error": "Choose Confirm or Reject for a valid account."}, HTTPStatus.BAD_REQUEST)
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
                {"message": "Admin registration approved. The email verification code was sent."}
            )
            return
        self.send_json(
            {"message": "Account confirmed." if result == "student_approved" else "Account rejected."}
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
