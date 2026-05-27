# FaceAuth - Enterprise Facial Recognition Authentication

A secure, cloud-powered facial recognition authentication interface built with Flask for access control and enterprise monitoring.

## 📋 Features

- **Real-Time Facial Recognition** - AI-powered identity verification
- **Responsive Design** - Mobile-optimized interface with modern dark theme
- **Enterprise Security** - CSRF protection, secure session management
- **Admin Dashboard Ready** - Comprehensive monitoring capabilities
- **Cloud-Native Architecture** - Designed for AWS Lambda, DynamoDB, S3

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- pip (Python package manager)

### Installation

1. **Clone or navigate to the project directory**

2. **Create a virtual environment**
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

3. **Install dependencies**
   ```powershell
   pip install -r requirements.txt
   ```

4. **Setup environment variables**
   ```powershell
   cp .env.example .env
   ```
   Edit `.env` if needed (defaults work for development)

5. **Run the application**
   ```powershell
   python app.py
   ```

6. **Open in browser**
   - Landing Page: `http://127.0.0.1:5000`
   - Login Page: `http://127.0.0.1:5000/login`

## 📁 Project Structure

```
FaceAuth/
├── app.py                 # Main Flask application
├── config.py             # Configuration management
├── requirements.txt      # Python dependencies
├── .env.example          # Environment variables template
├── .gitignore           # Git ignore rules
├── README.md            # This file
├── templates/
│   ├── base.html        # Base template with navigation
│   ├── landing.html     # Hero/marketing page
│   ├── login.html       # Login form
│   ├── 404.html         # Not found page
│   └── 500.html         # Server error page
└── static/
    ├── css/
    │   └── style.css    # Complete styling (680 lines)
    └── js/
        └── main.js      # Navigation toggle
```

## 🔧 Configuration

Environment variables in `.env`:
```
FLASK_ENV=development    # development or production
DEBUG=True              # Debug mode
SECRET_KEY=xxx          # Session secret (auto-generated)
FLASK_HOST=127.0.0.1   # Server host
FLASK_PORT=5000        # Server port
```

## 🔐 Security Features

- ✅ CSRF Protection (Flask-WTF)
- ✅ Secure Session Management
- ✅ Input Validation
- ✅ Password Field Masking
- ✅ Secure Session Cookies
- ✅ Environment Variable Configuration

## ⚠️ Development vs Production

### Development (Default)
```powershell
python app.py
```
- Uses Flask development server
- Debug mode enabled
- Automatic reloader (disabled for stability)

### Production
```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```
- Uses Gunicorn production server
- Runs 4 worker processes
- No debug mode
- Suitable for deployment

**Note**: The Flask development server warning is normal and informational.

## 🧪 Testing Routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Landing page with features |
| `/login` | GET/POST | Login form |
| `/login` | POST | Form submission handler |

## 🐛 Troubleshooting

### "ModuleNotFoundError: No module named 'flask'"
```powershell
pip install -r requirements.txt
```

### "Address already in use"
```powershell
# Change port in .env file
FLASK_PORT=5001
```

### CSRF Token Error
Ensure you're using the login form with `{{ csrf_token() }}` in templates

## 📦 Dependencies

- **Flask 3.0.3** - Web framework
- **Flask-WTF 1.2.1** - Form protection & CSRF
- **python-dotenv 1.0.0** - Environment management
- **Werkzeug 3.1.8** - WSGI utilities

## 🎨 UI/UX

- **Color Theme**: Dark mode with cyan/blue accents
- **Typography**: Inter font family with monospace accents
- **Responsiveness**: Mobile-first design (breakpoints: 980px, 760px)
- **Accessibility**: ARIA labels, keyboard navigation, reduced motion support

## 🚢 Deployment

### Heroku
1. Add `Procfile`:
   ```
   web: gunicorn app:app
   ```

2. Deploy:
   ```bash
   heroku login
   git push heroku main
   ```

### Docker (Coming Soon)
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
```

## 🤝 Contributing

1. Create a branch (`git checkout -b feature/improvement`)
2. Make changes and test
3. Commit changes (`git commit -am 'Add feature'`)
4. Push to branch (`git push origin feature/improvement`)
5. Create Pull Request

## 📝 License

FYP Project - APU Semester VI

## 🔗 Related Links

- [Flask Documentation](https://flask.palletsprojects.com/)
- [AWS Architecture](https://aws.amazon.com/)
- [Facial Recognition Technologies](https://en.wikipedia.org/wiki/Facial_recognition_system)
