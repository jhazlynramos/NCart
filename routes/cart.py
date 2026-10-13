from flask import Blueprint

from .route_helpers import register_legacy_routes
from app import NCartHandler as CoreNCartHandler

cart_bp = Blueprint("cart", __name__)
register_legacy_routes(
    cart_bp,
    [
        ("/cart", ["GET"]),
        ("/api/cart", ["GET", "POST"]),
        ("/api/cart/count", ["GET"]),
        ("/api/cart/<path:cart_item_id>", ["PATCH", "PUT", "DELETE"]),
    ],
)


class CartRoutesMixin:
    current_cart_user = CoreNCartHandler.current_cart_user
    handle_cart_get = CoreNCartHandler.handle_cart_get
    handle_cart_count = CoreNCartHandler.handle_cart_count
    handle_cart_add = CoreNCartHandler.handle_cart_add
    handle_cart_update = CoreNCartHandler.handle_cart_update
    handle_cart_remove = CoreNCartHandler.handle_cart_remove
