"""Vercel serverless entry point.

Vercel detects the module level `app` WSGI callable and serves every request
that is routed to this function with it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401