from http.server import SimpleHTTPRequestHandler

from .admin import AdminRoutesMixin
from .auth import AuthRoutesMixin
from .common import CommonRoutesMixin
from .complaints import ComplaintsRoutesMixin
from .cart import CartRoutesMixin
from .dispatcher import RequestDispatchMixin
from .favorites import FavoritesRoutesMixin
from .marketplace import MarketplaceRoutesMixin
from .messages import MessagesRoutesMixin
from .orders import OrdersRoutesMixin
from .posted_items import PostedItemsRoutesMixin
from .profile import ProfileRoutesMixin
from .shared import ROOT


class NCartHandler(
    RequestDispatchMixin,
    AuthRoutesMixin,
    MarketplaceRoutesMixin,
    PostedItemsRoutesMixin,
    FavoritesRoutesMixin,
    CartRoutesMixin,
    MessagesRoutesMixin,
    OrdersRoutesMixin,
    ComplaintsRoutesMixin,
    ProfileRoutesMixin,
    AdminRoutesMixin,
    CommonRoutesMixin,
    SimpleHTTPRequestHandler,
):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)
