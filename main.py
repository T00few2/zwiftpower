import functions_framework
from flask import Response, request

@functions_framework.http
def app(request):
    # Check if the request is for /favicon.ico
    if request.path == '/favicon.ico':
        return Response(status=204)
    return "Hello, world!"
