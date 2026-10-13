from .shared import *


class CommonRoutesMixin:
    def send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def current_account(self):
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookies.get("ncart_session")
        if not token:
            return None
        session = SESSIONS.get(token.value)
        if session:
            try:
                return find_user_by_id(session["id"])
            except Error:
                return None
        session = read_signed_session(token.value)
        if not session:
            return None
        try:
            return find_user_by_id(session["id"])
        except Error:
            return None

    def current_user(self):
        user = self.current_account()
        return (
            user
            if user
            and user.get("status") == "active"
            and user.get("account_status") in {"active", "verified"}
            and user.get("approval_status") == "approved"
            else None
        )

    def redirect_with_message(self, path, message, kind, extra=None, session_user_id=None):
        parameters = {kind: message}
        if extra:
            parameters.update(extra)
        query = urlencode(parameters)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"{path}?{query}")
        if session_user_id is not None:
            self.send_header(
                "Set-Cookie",
                f"ncart_session={make_session_cookie(session_user_id)}; Path=/; HttpOnly; SameSite=Lax",
            )
        self.end_headers()

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")
