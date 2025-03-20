import functions_framework

@functions_framework.http
def hello_world(request):
    """HTTP Cloud Function.
    Args:
        request (flask.Request): The request object.
    Returns:
        A string with "Hello, world!".
    """
    return "Hello, world!"
