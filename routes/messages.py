from flask import Blueprint

from .route_helpers import register_legacy_routes
from .shared import *


messages_bp = Blueprint("messages", __name__)
register_legacy_routes(
    messages_bp,
    [
        ("/messages", ["GET"]),
        ("/api/messages", ["GET", "POST"]),
    ],
)


class MessagesRoutesMixin:
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
            try:
                listing_id = int(listing_value) if listing_value else None
            except ValueError:
                listing_id = None
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
