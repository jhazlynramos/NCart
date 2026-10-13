from flask import Blueprint

from .route_helpers import register_legacy_routes
from .shared import *


posted_items_bp = Blueprint("posted_items", __name__)
register_legacy_routes(
    posted_items_bp,
    [
        ("/listings", ["GET"]),
        ("/api/listings", ["POST"]),
        ("/api/listings/<path:listing_id>", ["PUT"]),
    ],
)


class PostedItemsRoutesMixin:
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

    def save_listing_image(self, image_data):
        return self.save_uploaded_image(image_data, "listing")

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
