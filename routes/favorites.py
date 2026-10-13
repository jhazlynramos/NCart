from flask import Blueprint

from .route_helpers import register_legacy_routes
from .shared import *


favorites_bp = Blueprint("favorites", __name__)
register_legacy_routes(
    favorites_bp,
    [
        ("/favorites", ["GET"]),
        ("/api/favorites", ["GET", "POST"]),
        ("/api/favorites/<path:listing_id>", ["DELETE"]),
    ],
)


class FavoritesRoutesMixin:
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
