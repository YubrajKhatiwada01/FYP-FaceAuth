@echo off
REM FaceAuth - Production Server Launcher
REM No Flask warning - uses Gunicorn instead

echo Starting FaceAuth with Gunicorn...
echo.
echo 🚀 Server running at http://127.0.0.1:5000
echo Press CTRL+C to stop
echo.

gunicorn -w 4 -b 127.0.0.1:5000 --timeout 120 app:app
