@echo off
echo.
echo  FaceAuth DEV MODE on http://127.0.0.1:5000
echo  Press CTRL+C to stop
echo.
set FLASK_ENV=development
set FLASK_DEBUG=1
python -m flask --app app run --host=127.0.0.1 --port=5000 --debug
pause
