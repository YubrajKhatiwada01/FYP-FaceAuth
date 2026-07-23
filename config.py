import os
from datetime import timedelta

class Config:
    """Base configuration"""
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'

    FLASK_ENV = os.environ.get('FLASK_ENV', 'development')
    DEBUG = os.environ.get('DEBUG', 'True').lower() in ('true', '1', 'yes')

    # Session configuration
    PERMANENT_SESSION_LIFETIME = timedelta(hours=24)

    # FIX: Convert string 'False'/'True' from .env properly to a real boolean.
    # os.environ always returns strings — 'False' is truthy in Python!
    _secure_cookie = os.environ.get('SESSION_COOKIE_SECURE', 'False')
    SESSION_COOKIE_SECURE = _secure_cookie.lower() in ('true', '1', 'yes')

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'

    # CSRF — disable expiry so token doesn't expire during a long session
    WTF_CSRF_TIME_LIMIT = None

    # Server configuration
    # Default: localhost only. Override via FLASK_HOST in .env.
    # Set FLASK_HOST=0.0.0.0 for production (EC2 / external access).
    FLASK_HOST = os.environ.get('FLASK_HOST', '127.0.0.1')
    FLASK_PORT = int(os.environ.get('FLASK_PORT', 5000))


class DevelopmentConfig(Config):
    """Development configuration — runs on localhost only."""
    DEBUG = True
    TESTING = False
    SESSION_COOKIE_SECURE = False   # HTTP only in dev
    FLASK_HOST = '127.0.0.1'        # never expose dev server to the network


class ProductionConfig(Config):
    """Production configuration — set FLASK_HOST=0.0.0.0 in .env for EC2 access."""
    DEBUG = False
    # Set SESSION_COOKIE_SECURE=True in .env only when HTTPS is configured.


class TestingConfig(Config):
    """Testing configuration"""
    TESTING = True
    WTF_CSRF_ENABLED = False


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
