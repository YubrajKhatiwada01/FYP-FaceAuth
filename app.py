from flask import Flask, render_template, request
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
import os
from config import config

# Load environment variables from .env file
load_dotenv()

# Create Flask app
app = Flask(__name__)

# Load configuration from environment
config_name = os.environ.get('FLASK_ENV', 'development')
app.config.from_object(config[config_name])

# Enable CSRF protection
csrf = CSRFProtect(app)


# Make csrf_token available in all templates
@app.context_processor
def inject_csrf_token():
    """Inject CSRF token into template context"""
    from flask_wtf.csrf import generate_csrf
    return {'csrf_token': generate_csrf}


@app.get("/")
def landing():
    """Render the landing/home page"""
    return render_template("landing.html", active_page="home")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Handle login page and form submission"""
    from flask_wtf.csrf import generate_csrf
    
    message = None

    if request.method == "POST":
        operator_id = request.form.get("operator_id", "").strip()
        access_token = request.form.get("access_token", "").strip()

        if operator_id and access_token:
            message = {
                "type": "success",
                "text": "Access request received. Backend verification will be connected next.",
            }
        else:
            message = {
                "type": "error",
                "text": "Enter both Operator ID and Access Token to initialize access.",
            }

    return render_template("login.html", active_page="login", message=message, csrf_token=generate_csrf(), body_class="login-body")


@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors"""
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    return render_template("500.html"), 500


if __name__ == "__main__":
    host = app.config['FLASK_HOST']
    port = app.config['FLASK_PORT']
    debug = app.config['DEBUG']
    
    print(f"🚀 Starting FaceAuth on http://{host}:{port}")
    print(f"📋 Environment: {config_name.upper()}")
    print()
    
    # Use waitress instead of Flask development server
    from waitress import serve
    serve(app, host=host, port=port)

