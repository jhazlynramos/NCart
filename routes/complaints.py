from flask import Blueprint

from .route_helpers import register_legacy_routes
from .shared import *


complaints_bp = Blueprint("complaints", __name__)
register_legacy_routes(
    complaints_bp,
    [
        ("/reports", ["GET"]),
        ("/api/reports", ["GET", "POST"]),
    ],
)


class ComplaintsRoutesMixin:
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
