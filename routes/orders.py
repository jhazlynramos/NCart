import logging

from flask import Blueprint

from .route_helpers import register_legacy_routes
from .shared import *

logger = logging.getLogger(__name__)

orders_bp = Blueprint("orders", __name__)
register_legacy_routes(
    orders_bp,
    [
        ("/orders", ["GET"]),
        ("/api/orders", ["GET", "POST"]),
        ("/api/orders/<path:order_route>", ["GET", "POST", "PUT", "DELETE"]),
    ],
)


class OrdersRoutesMixin:
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
            order["created_at"] = order["created_at"].isoformat()
            order["can_seller_cancel"] = bool(order["can_seller_cancel"])
            for field in ("payment_submitted_at", "payment_verified_at"):
                if order[field]:
                    order[field] = order[field].isoformat()
        self.send_json({"viewer_id": int(user["id"]), "orders": orders, "sales": sales})

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
