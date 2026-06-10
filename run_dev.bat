@echo off
REM FaceAuth - Development Server Launcher
REM Runs Flask in development mode with auto-reload

echo Starting FaceAuth in Development Mode...
echo.
echo 🚀 Server running at http://127.0.0.1:5000
echo Press CTRL+C to stop
echo.

set FLASK_ENV=development
set FLASK_DEBUG=1
python -m flask run --host=127.0.0.1 --port=5000
