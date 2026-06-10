@echo off
REM FaceAuth - Production Server Launcher
REM Uses Waitress (Windows-compatible alternative to Gunicorn)

echo Starting FaceAuth with Waitress...
echo.
echo 🚀 Server running at http://127.0.0.1:5000
echo Press CTRL+C to stop
echo.

waitress-serve --host=127.0.0.1 --port=5000 --threads=4 app:app
