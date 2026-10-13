from .shared import *


class RequestDispatchMixin:
    def do_GET(self):
        route = urlparse(self.path).path
        account = self.current_account()
        protected_pages = {
            "/admin",
            "/home",
            "/dashboard",
            "/profile",
            "/listings",
            "/messages",
            "/orders",
            "/favorites",
            "/cart",
            "/reports",
            "/templates/home.html",
            "/templates/admin.html",
            "/templates/profile.html",
            "/templates/listings.html",
            "/templates/messages.html",
            "/templates/orders.html",
            "/templates/favorites.html",
            "/templates/cart.html",
            "/templates/reports.html",
        }
        if account and route in protected_pages:
            account_is_active = (
                account.get("status") == "active"
                and account.get("account_status") in {"active", "verified"}
                and account.get("approval_status") == "approved"
            )
            if not account_is_active:
                if account.get("role") != "admin":
                    state = account.get("status")
                    if state not in {"pending", "rejected"}:
                        state = "pending"
                    destination = f"/pending-account?state={state}"
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
        if route == "/api/profile":
            self.handle_profile_get()
            return
        if route == "/api/messages":
            self.handle_messages_get()
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
        elif route in {
            "/marketplace",
            "/favorites",
            "/listings",
            "/messages",
            "/orders",
            "/reports",
            "/profile",
            "/cart",
        }:
            query = urlparse(self.path).query
            self.path = f"/templates{route}.html" + (f"?{query}" if query else "")
        super().do_GET()

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
                self.send_json(
                    {"error": str(error) or "Invalid order request."},
                    HTTPStatus.BAD_REQUEST,
                )
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
        if route.startswith("/api/orders/"):
            order_route = route.removeprefix("/api/orders/").split("/")
            if len(order_route) == 2 and order_route[1] in {"payment", "payment-review"}:
                self.handle_order_payment_post(order_route[0], order_route[1])
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
            account_id = route.removeprefix(
                "/api/admin/account-confirmations/"
            ).removesuffix("/review").strip("/")
            self.handle_pending_account_review(account_id, values)
            return
        if route.startswith("/api/admin/account-confirmations/") and route.endswith("/retry-otp"):
            account_id = route.removeprefix(
                "/api/admin/account-confirmations/"
            ).removesuffix("/retry-otp").strip("/")
            self.handle_admin_registration_otp_retry(account_id)
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
