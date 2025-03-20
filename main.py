# main.py
from flask import Flask, request

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def hello_world():
    if request.method == 'POST':
        request_json = request.get_json()
        if request_json and 'name' in request_json:
            name = request_json['name']
        else:
            name = 'World'
        return f'Hello {name}!'
    else:
        return 'Hello World!'