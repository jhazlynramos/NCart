from flask import Blueprint

from .route_helpers import register_legacy_routes
from .shared import *


marketplace_bp = Blueprint("marketplace", __name__)
register_legacy_routes(
    marketplace_bp,
    [
        ("/marketplace", ["GET"]),
        ("/api/listings", ["GET"]),
    ],
)


class MarketplaceRoutesMixin:
    def handle_listings_get(self):
        user = self.current_user()
        query = parse_qs(urlparse(self.path).query)
        mine = query.get("mine", [""])[0] == "1"
        if mine and user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        if not mine and "listing" in query:
            try:
                listing_id = int(query["listing"][0])
                if listing_id < 1:
                    raise ValueError
                listing = find_marketplace_listing_by_id(listing_id)
            except (ValueError, IndexError):
                self.send_json({"error": "Invalid listing."}, HTTPStatus.BAD_REQUEST)
                return
            except Error:
                self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if listing is None:
                self.send_json({"error": "Listing is no longer available."}, HTTPStatus.NOT_FOUND)
                return
            listing["id"] = int(listing["id"])
            listing["seller_id"] = int(listing["seller_id"])
            listing["quantity"] = int(listing["quantity"])
            listing["price"] = str(listing["price"])
            listing["created_at"] = listing["created_at"].isoformat()
            self.send_json(
                {
                    "viewer_id": int(user["id"]) if user else None,
                    "listings": [listing],
                    "categories": [listing["category"]],
                    "pagination": {
                        "page": 1, "per_page": 1,
                        "total_items": 1, "total_pages": 1,
                    },
                }
            )
            return
        if not mine and "page" in query:
            try:
                page = int(query["page"][0])
                if page < 1:
                    raise ValueError
                category = query.get("category", [""])[0].strip()
                search = query.get("search", [""])[0].strip()
                if len(category) > 80 or len(search) > 200:
                    raise ValueError
                listings, total_count, categories = find_marketplace_listings(
                    page, 6, category, search
                )
            except (ValueError, IndexError):
                self.send_json({"error": "Invalid marketplace page or filter."}, HTTPStatus.BAD_REQUEST)
                return
            except Error:
                self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            total_pages = (total_count + 5) // 6
            if page > max(total_pages, 1):
                page = max(total_pages, 1)
                listings, total_count, categories = find_marketplace_listings(
                    page, 6, category, search
                )
                total_pages = (total_count + 5) // 6
            for listing in listings:
                listing["id"] = int(listing["id"])
                listing["seller_id"] = int(listing["seller_id"])
                listing["quantity"] = int(listing["quantity"])
                listing["price"] = str(listing["price"])
                listing["created_at"] = listing["created_at"].isoformat()
            self.send_json(
                {
                    "viewer_id": int(user["id"]) if user else None,
                    "listings": listings,
                    "categories": categories,
                    "pagination": {
                        "page": page,
                        "per_page": 6,
                        "total_items": total_count,
                        "total_pages": total_pages,
                    },
                }
            )
            return
        try:
            listings = find_listings(user["id"] if mine else None)
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        for listing in listings:
            listing["id"] = int(listing["id"])
            listing["quantity"] = int(listing["quantity"])
            listing["price"] = str(listing["price"])
            listing["created_at"] = listing["created_at"].isoformat()
        self.send_json({"viewer_id": int(user["id"]) if user else None, "listings": listings})
