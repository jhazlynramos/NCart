from flask import Blueprint

from .route_helpers import register_legacy_routes
from .shared import *


profile_bp = Blueprint("profile", __name__)
register_legacy_routes(
    profile_bp,
    [
        ("/profile", ["GET"]),
        ("/api/profile", ["GET", "POST", "PUT"]),
    ],
)


class ProfileRoutesMixin:
    def handle_profile_get(self):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        self.send_json({"profile": user})

    def handle_profile_update(self, body):
        user = self.current_user()
        if user is None:
            self.send_json({"error": "Please log in first."}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            values = json.loads(body)
            if not isinstance(values, dict):
                raise ValueError
            full_name = str(values.get("full_name", "")).strip()
            bio = str(values.get("bio", "")).strip()
            location = str(values.get("location", "")).strip()
            username = str(values.get("username", "")).strip().lower()
            image_data = str(values.get("image_data", "")).strip()
            cover_image_data = str(values.get("cover_image_data", "")).strip()
            if (
                not full_name
                or len(full_name) > 120
                or len(bio) > 500
                or len(location) > 160
                or not re.fullmatch(r"[a-z0-9_]{3,32}", username)
            ):
                raise ValueError
            image_url = self.save_uploaded_image(image_data, "profile") if image_data else None
            cover_image_url = (
                self.save_uploaded_image(cover_image_data, "cover")
                if cover_image_data else None
            )
            if not update_user_profile(
                user["id"], full_name, bio, location, username,
                image_url, cover_image_url,
            ):
                self.send_json({"error": "Profile was not found."}, HTTPStatus.NOT_FOUND)
                return
            updated_profile = find_user_by_id(user["id"])
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json(
                {"error": "Enter a valid name, username, and profile information."},
                HTTPStatus.BAD_REQUEST,
            )
            return
        except IntegrityError as error:
            if error.errno == 1062:
                self.send_json({"error": "That username is already in use."}, HTTPStatus.CONFLICT)
                return
            self.send_json({"error": "Could not save your profile."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except Error:
            self.send_json({"error": "Database connection failed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_json({"profile": updated_profile})
