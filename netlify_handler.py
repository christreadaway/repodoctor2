"""
Netlify Functions handler — wraps the Flask app for serverless execution.
Uses awsgi to translate API Gateway events into WSGI requests.
"""

import os
import sys

# Ensure the function directory is on the Python path
func_dir = os.path.dirname(os.path.abspath(__file__))
if func_dir not in sys.path:
    sys.path.insert(0, func_dir)

import awsgi
import models

# Ensure data directories exist (ephemeral in serverless, recreated each cold start)
models._ensure_dirs()

from app import app

# In serverless mode, disable debug and use environment secret key
app.debug = False
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-in-netlify-env")


def handler(event, context):
    """AWS Lambda / Netlify Functions entry point."""
    return awsgi.response(app, event, context)
