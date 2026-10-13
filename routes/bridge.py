from email.message import Message
from io import BytesIO

from flask import Response, request

from .handler import NCartHandler
from .shared import ROOT


class FlaskRequestHandler(NCartHandler):
    def send_response(self, code, message=None):
        self.status = int(code)
        self.response_headers.append(("Server", self.version_string()))
        self.response_headers.append(("Date", self.date_time_string()))

    def send_header(self, keyword, value):
        self.response_headers.append((keyword, str(value)))

    def end_headers(self):
        self.headers_sent = True


def dispatch_legacy():
    handler = object.__new__(FlaskRequestHandler)
    handler.path = request.path
    if request.query_string:
        handler.path += "?" + request.query_string.decode("latin-1")
    handler.command = request.method
    handler.request_version = request.environ.get("SERVER_PROTOCOL", "HTTP/1.1")
    handler.requestline = f"{request.method} {handler.path} {handler.request_version}"
    handler.client_address = (request.remote_addr or "", 0)
    handler.directory = str(ROOT)
    handler.headers = Message()
    for name, value in request.headers.items():
        handler.headers.add_header(name, value)
    body = request.get_data(cache=True)
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.response_headers = []
    handler.headers_sent = False
    handler.close_connection = True

    method = getattr(handler, f"do_{request.method}", None)
    if method is None:
        handler.send_error(501, "Unsupported method")
    else:
        method()

    response = Response(handler.wfile.getvalue(), status=handler.status)
    for name, value in handler.response_headers:
        if name.lower() == "set-cookie":
            response.headers.add(name, value)
        else:
            response.headers.set(name, value)
    return response
