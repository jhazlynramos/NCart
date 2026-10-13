import smtplib
import unittest
from email.message import EmailMessage
from unittest.mock import patch

import app as http_app
import db
import routes.auth as auth_routes
import routes.admin as admin_routes


class FakeCursor:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.current = None
        self.statements = []
        self.rowcount = 0
        self.lastrowid = 42

    def execute(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        self.current = self.responses.pop(0) if self.responses else None
        self.rowcount = 1

    def fetchone(self):
        return self.current

    def fetchall(self):
        return self.current or []

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self, **_kwargs):
        return self.fake_cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class RegistrationHandler(auth_routes.AuthRoutesMixin):
    def __init__(self):
        self.responses = []

    def send_json(self, payload, status=200):
        self.responses.append((payload, status))


class AdminHandler(admin_routes.AdminRoutesMixin, auth_routes.AuthRoutesMixin):
    def __init__(self):
        self.responses = []

    def authenticated_admin(self):
        return {"id": 7, "role": "admin"}

    def send_json(self, payload, status=200):
        self.responses.append((payload, status))


class HttpRegistrationHandler(http_app.NCartHandler):
    def __init__(self):
        self.responses = []

    def send_json(self, payload, status=200):
        self.responses.append((payload, status))


class AdminRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.registration = {
            "account_type": ["admin"],
            "first_name": ["Ari"],
            "middle_name": ["M."],
            "last_name": ["Applicant"],
            "email": ["ari@example.edu"],
            "password": ["NcartTest2026!"],
            "confirm_password": ["NcartTest2026!"],
        }

    def test_admin_registration_persists_pending_request_without_otp_or_email(self):
        handler = RegistrationHandler()
        with (
            patch.object(auth_routes, "find_user_by_email", return_value=None),
            patch.object(auth_routes, "create_pending_admin_registration", return_value=42) as create_request,
            patch.object(auth_routes, "issue_admin_registration_otp") as issue_otp,
            patch.object(
                auth_routes.AuthRoutesMixin,
                "send_admin_registration_otp_email",
            ) as send_email,
        ):
            handler.handle_admin_registration_send_otp(self.registration)

        create_request.assert_called_once()
        issue_otp.assert_not_called()
        send_email.assert_not_called()
        message, status = handler.responses[-1]
        self.assertEqual(status, 202)
        self.assertIn("Please wait for administrator approval", message["message"])

    def test_http_server_admin_registration_uses_the_same_approval_first_flow(self):
        handler = HttpRegistrationHandler()
        with (
            patch.object(http_app, "find_user_by_email", return_value=None),
            patch.object(http_app, "create_pending_admin_registration", return_value=42),
            patch.object(http_app, "issue_admin_registration_otp") as issue_otp,
            patch.object(
                http_app.NCartHandler,
                "send_admin_registration_otp_email",
            ) as send_email,
        ):
            handler.handle_admin_registration_send_otp(self.registration)

        self.assertEqual(handler.responses[-1][1], 202)
        issue_otp.assert_not_called()
        send_email.assert_not_called()

    def test_student_registration_still_sends_existing_otp(self):
        handler = RegistrationHandler()
        student = {
            **self.registration,
            "account_type": ["student"],
            "student_number": ["0000-0000"],
        }
        with (
            patch.object(auth_routes, "find_user_by_email", return_value=None),
            patch.object(auth_routes, "find_user_by_student_number", return_value=None),
            patch.object(auth_routes, "issue_admin_registration_otp", return_value=("sent", 0)) as issue_otp,
            patch.object(
                auth_routes.AuthRoutesMixin,
                "send_admin_registration_otp_email",
            ) as send_email,
        ):
            handler.handle_admin_registration_send_otp(student)

        self.assertEqual(handler.responses[-1][1], 202)
        self.assertEqual(issue_otp.call_args.args[6], "student")
        self.assertEqual(send_email.call_args.args[2], "student")

    def test_admin_approval_sends_otp_only_after_approval_transition(self):
        handler = AdminHandler()
        applicant = {
            "email": "ari@example.edu",
            "full_name": "Ari M. Applicant",
            "first_name": "Ari",
            "middle_name": "M.",
            "last_name": "Applicant",
            "password_hash": "stored-hash",
        }
        with (
            patch.object(admin_routes, "review_pending_account", return_value="admin_approved"),
            patch.object(auth_routes, "claim_admin_registration_otp_delivery", return_value=applicant),
            patch.object(auth_routes, "issue_admin_registration_otp", return_value=("sent", 0)),
            patch.object(auth_routes, "update_admin_otp_delivery_status", return_value=True),
            patch.object(
                auth_routes.AuthRoutesMixin,
                "send_admin_registration_otp_email",
            ) as send_email,
        ):
            handler.handle_pending_account_review("42", {"decision": "approved"})

        self.assertEqual(handler.responses[-1][1], 200)
        self.assertEqual(send_email.call_args.args[2], "admin")

    def test_admin_email_delivery_failure_is_reported_and_keeps_retry_state(self):
        handler = RegistrationHandler()
        applicant = {
            "email": "ari@example.edu",
            "full_name": "Ari M. Applicant",
            "first_name": "Ari",
            "middle_name": "M.",
            "last_name": "Applicant",
            "password_hash": "stored-hash",
        }
        with (
            patch.object(auth_routes, "claim_admin_registration_otp_delivery", return_value=applicant),
            patch.object(auth_routes, "issue_admin_registration_otp", return_value=("sent", 0)),
            patch.object(auth_routes, "invalidate_admin_registration_otp") as invalidate,
            patch.object(auth_routes, "update_admin_otp_delivery_status") as update_delivery,
            patch.object(
                auth_routes.AuthRoutesMixin,
                "send_admin_registration_otp_email",
                side_effect=smtplib.SMTPException("delivery failed"),
            ),
        ):
            state, _ = handler.send_approved_admin_registration_otp(42, 7, "not_sent")

        self.assertEqual(state, "delivery_failed")
        invalidate.assert_called_once()
        update_delivery.assert_called_with("ari@example.edu", "failed")

    def test_authorized_admin_can_retry_a_failed_otp_delivery(self):
        handler = AdminHandler()
        applicant = {
            "email": "ari@example.edu",
            "full_name": "Ari M. Applicant",
            "first_name": "Ari",
            "middle_name": "M.",
            "last_name": "Applicant",
            "password_hash": "stored-hash",
        }
        with (
            patch.object(auth_routes, "claim_admin_registration_otp_delivery", return_value=applicant) as claim,
            patch.object(auth_routes, "issue_admin_registration_otp", return_value=("sent", 0)),
            patch.object(auth_routes, "update_admin_otp_delivery_status", return_value=True),
            patch.object(
                auth_routes.AuthRoutesMixin,
                "send_admin_registration_otp_email",
            ) as send_email,
        ):
            handler.handle_admin_registration_otp_retry("42")

        claim.assert_called_once_with(42, 7, "failed")
        send_email.assert_called_once()
        self.assertEqual(handler.responses[-1][1], 200)

    def test_unauthorized_admin_review_request_does_not_reach_database(self):
        handler = AdminHandler()
        def deny_admin():
            handler.send_json({"error": "Please log in first."}, 401)
            return None

        with (
            patch.object(handler, "authenticated_admin", side_effect=deny_admin),
            patch.object(admin_routes, "review_pending_account") as review,
        ):
            handler.handle_pending_account_review("42", {"decision": "approved"})

        review.assert_not_called()
        self.assertEqual(handler.responses[-1][1], 401)

    def test_approved_admin_email_contains_warning_expiry_and_verification_link(self):
        class FakeSmtp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def starttls(self):
                pass

            def login(self, *_args):
                pass

            def send_message(self, message):
                self.message = message

        smtp = FakeSmtp()
        with (
            patch.dict(
                "os.environ",
                {
                    "NCART_SMTP_USERNAME": "sender@example.edu",
                    "NCART_SMTP_PASSWORD": "smtp-password",
                    "NCART_PUBLIC_BASE_URL": "https://ncart.example.edu",
                },
            ),
            patch.object(auth_routes.smtplib, "SMTP", return_value=smtp),
        ):
            auth_routes.AuthRoutesMixin.send_admin_registration_otp_email(
                "ari@example.edu",
                "123456",
                "admin",
            )

        self.assertIsInstance(smtp.message, EmailMessage)
        content = smtp.message.get_content()
        self.assertIn("NCart", content)
        self.assertIn("registration has been approved", content)
        self.assertIn("123456", content)
        self.assertIn("expires in five minutes", content)
        self.assertIn("Do not share this code", content)
        self.assertIn("https://ncart.example.edu/templates/create-account.html", content)
        self.assertNotIn("123456", content.split("Verification page:")[-1])

    def test_otp_cannot_activate_admin_without_approved_request(self):
        otp_record = {
            "attempt_count": 0,
            "expires_at": 2_000_000_000,
            "otp_hash": "valid-hash",
            "account_type": "admin",
        }
        cursor = FakeCursor([otp_record, None])
        connection = FakeConnection(cursor)
        with patch.object(db, "get_connection", return_value=connection):
            result = db.complete_admin_registration("ari@example.edu", "valid-hash", 1_900_000_000)

        self.assertEqual(result, ("not_approved", None))
        self.assertEqual(connection.commits, 0)
        self.assertTrue(any("admin_request_status = 'approved'" in query for query, _ in cursor.statements))
        self.assertFalse(any("SET role = 'admin'" in query for query, _ in cursor.statements))

    def test_invalid_expired_and_reused_otps_are_rejected(self):
        invalid_record = {
            "attempt_count": 0,
            "expires_at": 2_000_000_000,
            "otp_hash": "other-hash",
            "account_type": "admin",
        }
        invalid_cursor = FakeCursor([invalid_record])
        with patch.object(db, "get_connection", return_value=FakeConnection(invalid_cursor)):
            invalid = db.complete_admin_registration("ari@example.edu", "wrong-hash", 1_900_000_000)
        self.assertEqual(invalid, ("invalid", None))
        self.assertTrue(any("attempt_count" in query for query, _ in invalid_cursor.statements))

        expired_record = {
            "attempt_count": 0,
            "expires_at": 1_800_000_000,
            "otp_hash": "valid-hash",
            "account_type": "admin",
        }
        expired_connection = FakeConnection(FakeCursor([expired_record]))
        with patch.object(db, "get_connection", return_value=expired_connection):
            expired = db.complete_admin_registration("ari@example.edu", "valid-hash", 1_900_000_000)
        self.assertEqual(expired, ("expired", None))

        reused_connection = FakeConnection(FakeCursor([None]))
        with patch.object(db, "get_connection", return_value=reused_connection):
            reused = db.complete_admin_registration("ari@example.edu", "valid-hash", 1_900_000_000)
        self.assertEqual(reused, ("not_found", None))

    def test_approved_admin_otp_activates_once_and_consumes_challenge(self):
        otp_record = {
            "attempt_count": 0,
            "expires_at": 2_000_000_000,
            "otp_hash": "valid-hash",
            "account_type": "admin",
        }
        approved_application = {"id": 42}
        pending_user = {
            "id": 42,
            "role": "buyer",
            "account_status": "pending",
            "status": "pending",
            "approval_status": "pending",
            "admin_request_status": "approved",
        }
        cursor = FakeCursor([otp_record, approved_application, pending_user])
        connection = FakeConnection(cursor)
        with patch.object(db, "get_connection", return_value=connection):
            result = db.complete_admin_registration("ari@example.edu", "valid-hash", 1_900_000_000)

        self.assertEqual(result, ("verified_admin", 42))
        self.assertEqual(connection.commits, 1)
        self.assertTrue(any("SET role = 'admin'" in query for query, _ in cursor.statements))
        self.assertTrue(any("DELETE FROM admin_registration_otps" in query for query, _ in cursor.statements))

    def test_reviewer_must_be_authenticated_admin_and_cannot_approve_self(self):
        unauthenticated_cursor = FakeCursor([None])
        unauthenticated_connection = FakeConnection(unauthenticated_cursor)
        with patch.object(db, "get_connection", return_value=unauthenticated_connection):
            self.assertFalse(db.review_pending_account(42, 7, "approved"))

        admin = {"id": 7}
        applicant = {"id": 7, "admin_request_submitted_at": "submitted", "admin_request_status": "pending"}
        self_approval_cursor = FakeCursor([admin, applicant])
        self_approval_connection = FakeConnection(self_approval_cursor)
        with patch.object(db, "get_connection", return_value=self_approval_connection):
            self.assertFalse(db.review_pending_account(7, 7, "approved"))
        self.assertEqual(self_approval_connection.commits, 0)
        self.assertEqual(len(self_approval_cursor.statements), 2)

    def test_student_approval_path_remains_separate(self):
        admin = {"id": 7}
        student = {"id": 42, "admin_request_submitted_at": None, "admin_request_status": "none"}
        cursor = FakeCursor([admin, student])
        connection = FakeConnection(cursor)
        with patch.object(db, "get_connection", return_value=connection):
            result = db.review_pending_account(42, 7, "approved")

        self.assertEqual(result, "student_approved")
        update_sql = cursor.statements[-1][0]
        self.assertIn("confirmed_by = %s", update_sql)
        self.assertIn("admin_request_submitted_at IS NULL", update_sql)
        self.assertEqual(connection.commits, 1)

    def test_admin_rejection_records_reviewer_without_approval_or_otp(self):
        admin = {"id": 7}
        applicant = {
            "id": 42,
            "admin_request_submitted_at": "submitted",
            "admin_request_status": "pending",
        }
        cursor = FakeCursor([admin, applicant])
        connection = FakeConnection(cursor)
        with patch.object(db, "get_connection", return_value=connection):
            result = db.review_pending_account(42, 7, "rejected")

        self.assertEqual(result, "admin_rejected")
        update_sql = cursor.statements[-1][0]
        self.assertIn("admin_request_status = 'rejected'", update_sql)
        self.assertIn("admin_request_reviewed_by = %s", update_sql)
        self.assertIn("admin_request_reviewed_at = CURRENT_TIMESTAMP", update_sql)
        self.assertIn("approval_status = 'rejected'", update_sql)
        self.assertNotIn("admin_registration_otps", update_sql)
        self.assertEqual(connection.commits, 1)

    def test_repeated_admin_approval_is_not_accepted_again(self):
        admin = {"id": 7}
        already_reviewed = {
            "id": 42,
            "admin_request_submitted_at": "submitted",
            "admin_request_status": "approved",
        }
        cursor = FakeCursor([admin, already_reviewed])
        connection = FakeConnection(cursor)
        with patch.object(db, "get_connection", return_value=connection):
            result = db.review_pending_account(42, 7, "approved")

        self.assertFalse(result)
        self.assertEqual(len(cursor.statements), 2)
        self.assertEqual(connection.commits, 0)


if __name__ == "__main__":
    unittest.main()
