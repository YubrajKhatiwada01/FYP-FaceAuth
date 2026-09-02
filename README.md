# FaceAuth - Two-Factor Physical Access Control System

FaceAuth is a web-based access control system that uses two verification steps before granting physical access:

1. **Bluetooth proximity check** - confirms the user's registered device is nearby
2. **Facial recognition** - verifies the user's identity using a live camera feed

The system is built with Flask on the backend. User data, access logs, and enrolled face photos are stored on AWS (DynamoDB and S3). When access is granted, an AWS Lambda function is triggered and the physical door is unlocked via an ESP32 device connected to AWS IoT Core.

---

## How the System Works

### Authentication Flow

```
User approaches door
        |
        v
[Step 1] Bluetooth Scan
  - Server scans for registered BLE devices nearby
  - Device must meet the RSSI signal threshold (default: -50 dBm)
  - If no matching device is found, access is denied
        |
        v
[Step 2] Facial Recognition
  - Browser captures a live camera frame
  - Frame is sent to the server as base64 image data
  - Server compares the face against the enrolled photo stored in S3
  - dlib computes a face encoding and checks distance (default tolerance: 0.55)
  - If face does not match, access is denied
        |
        v
[Access Granted]
  - Event is logged to DynamoDB
  - AWS Lambda post-auth trigger fires
  - MQTT message is published to AWS IoT Core
  - ESP32 receives the message and unlocks the door
```

### Admin Dashboard

Administrators log in with an Operator ID and password. The dashboard shows:

- Total access granted and denied counts
- Active users and access points
- 7-day access trend chart
- Recent audit log entries

Administrators can manage users (create, edit, deactivate, upload enrollment photos), view full access logs, configure access points, and adjust system settings.

---

## System Architecture

```
Browser (HTML + JS)
        |
        v
Flask Web Server (app.py)
  |         |         |         |
  v         v         v         v
AWS        AWS       AWS       AWS IoT Core
DynamoDB    S3       Lambda    (MQTT broker)
(users,   (enrolled  (post-auth      |
 logs,     photos)    trigger)       v
 access                         ESP32 Device
 points)                        (door unlock)

Bluetooth Scanner (bleak)
  - Runs on the same machine as Flask
  - Scans for BLE device MAC addresses registered to users
```

---

## Project Structure

```
FaceAuth/
|
|-- app.py                      # Main Flask application, all routes
|-- config.py                   # Flask configuration (dev/production)
|-- aws_config.py               # boto3 client factory, reads credentials from .env
|-- aws_dynamodb.py             # DynamoDB operations (users, logs, access points)
|-- aws_s3.py                   # S3 operations (upload/download enrolled photos)
|-- aws_lambda_client.py        # Invokes post-auth Lambda function
|-- aws_iot_door.py             # Publishes MQTT message to AWS IoT Core (door unlock)
|-- bluetooth_scanner.py        # BLE proximity scan using bleak
|-- face_recognition_service.py # Face encoding and verification using dlib
|-- requirements.txt            # Python dependencies
|-- .env.example                # Template for all required environment variables
|-- run.bat                     # Windows shortcut to start the server
|-- run_dev.bat                 # Windows shortcut for development mode
|
|-- lambda/
|   |-- lambda_function.py      # AWS Lambda post-auth trigger code
|   |-- deploy_lambda.bat       # Script to package and deploy Lambda
|   |-- requirements.txt        # Lambda-specific dependencies
|
|-- templates/
|   |-- base.html               # Shared layout with navigation
|   |-- dashboard_base.html     # Admin dashboard layout
|   |-- landing.html            # Public landing page
|   |-- login.html              # Admin login page
|   |-- dashboard.html          # Stats and recent activity
|   |-- authentication.html     # Live authentication screen (Bluetooth + face scan)
|   |-- bluetooth.html          # Bluetooth scan management
|   |-- users.html              # User management
|   |-- logs.html               # Access log viewer
|   |-- access_control.html     # Access point configuration
|   |-- settings.html           # System settings
|   |-- 404.html                # Not found error page
|   |-- 500.html                # Server error page
|
|-- static/
    |-- css/
    |   |-- style.css           # Application styles
    |-- js/
        |-- main.js             # Navigation and UI interactions
```

---

## Prerequisites

Before running the project, make sure you have the following installed:

- Python 3.8 or higher
- pip (Python package manager)
- A working webcam (for facial recognition)
- Bluetooth adapter on the server machine (for proximity scanning)
- An AWS account with the following set up:
  - DynamoDB tables: `faceauth-users`, `faceauth-logs`, `faceauth-access-points`
  - S3 bucket: `faceauth-fyp`
  - Lambda function: `faceauth-post-auth-trigger`
  - IoT Core with a configured device endpoint (for door unlock)

Note: The `face_recognition` library requires `dlib` which depends on CMake and a C++ compiler. On Windows, install [CMake](https://cmake.org/download/) and [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) before running `pip install`.

---

## Setup and Installation

### 1. Clone the repository

```powershell
git clone <repository-url>
cd FaceAuth
```

### 2. Create a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```powershell
pip install -r requirements.txt
```

Installing `face_recognition` and `dlib` can take several minutes. This is normal.

### 4. Configure environment variables

Copy the example file and fill in your values:

```powershell
copy .env.example .env
```

Open `.env` and set the required values. See the Configuration section below.

### 5. Run the application

```powershell
python app.py
```

Or use the provided batch file on Windows:

```powershell
.\run.bat
```

### 6. Open in browser

- Landing page: `http://127.0.0.1:5000`
- Admin login: `http://127.0.0.1:5000/login`

---

## Configuration

All configuration is done through the `.env` file. Copy `.env.example` to `.env` and set the values below.

### Flask Settings

```
FLASK_ENV=development       # Use 'production' for deployment
DEBUG=True                  # Set to False in production
SECRET_KEY=your-secret      # Any long random string
FLASK_HOST=127.0.0.1        # Use 0.0.0.0 to accept external connections
FLASK_PORT=5000
SESSION_COOKIE_SECURE=False # Set to True when using HTTPS
```

### AWS Credentials

```
AWS_ACCESS_KEY_ID=your-key
AWS_SECRET_ACCESS_KEY=your-secret
AWS_REGION=ap-south-1
```

### AWS Resource Names

```
S3_BUCKET_NAME=faceauth-fyp
DYNAMO_USERS_TABLE=faceauth-users
DYNAMO_LOGS_TABLE=faceauth-logs
DYNAMO_POINTS_TABLE=faceauth-access-points
LAMBDA_FUNCTION_NAME=faceauth-post-auth-trigger
```

### AWS IoT Core (Door Unlock)

```
IOT_ENDPOINT=your-endpoint.iot.ap-south-1.amazonaws.com
```

Find this value in the AWS IoT Console under Settings > Device data endpoint.

### Face Recognition Tuning

```
FACE_TOLERANCE=0.55         # Lower = stricter match (range: 0.4 to 0.6)
```

### Bluetooth Tuning

```
BT_RSSI_THRESHOLD=-50       # Minimum signal strength in dBm (closer = higher value)
BT_SCAN_DURATION=6.0        # How many seconds to scan for BLE devices
```

---

## Application Routes

| Route | Method | Access | Description |
|---|---|---|---|
| `/` | GET | Public | Landing page |
| `/login` | GET, POST | Public | Admin login |
| `/logout` | GET | Admin | End session |
| `/dashboard` | GET | Admin | Overview and stats |
| `/authentication` | GET | Admin | Run Bluetooth + face scan |
| `/bluetooth` | GET | Admin | Bluetooth scan management |
| `/users` | GET | Admin | User list and management |
| `/logs` | GET | Admin | Full access log |
| `/access-control` | GET | Admin | Access point configuration |
| `/settings` | GET, POST | Admin | System settings |
| `/api/dashboard-stats` | GET | Admin | Dashboard data (JSON) |
| `/api/bluetooth/scan` | POST | Admin | Trigger a BLE scan (JSON) |
| `/api/face/verify` | POST | Admin | Submit a frame for face check (JSON) |

---

## Security

- CSRF protection on all forms (Flask-WTF)
- Session-based authentication with secure cookies
- Passwords stored as hashed values (Werkzeug)
- AWS credentials read from environment variables only, never hard-coded
- All admin routes require an active login session

---

## Running in Production

Use Waitress (already in requirements) instead of the Flask development server:

```powershell
waitress-serve --host=0.0.0.0 --port=5000 app:app
```

Or use Gunicorn on Linux:

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

For production, also update `.env`:

```
FLASK_ENV=production
DEBUG=False
SESSION_COOKIE_SECURE=True
SECRET_KEY=<strong-random-string>
FLASK_HOST=0.0.0.0
```

---

## Troubleshooting

**"ModuleNotFoundError: No module named 'flask'"**

The virtual environment is not active or dependencies are not installed.

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**"ModuleNotFoundError: No module named 'face_recognition'"**

`dlib` failed to install. Make sure CMake and Visual Studio Build Tools are installed, then try again:

```powershell
pip install dlib
pip install face_recognition
```

**"Address already in use"**

Change the port in `.env`:

```
FLASK_PORT=5001
```

**Face recognition is slow on first scan**

This is expected. On startup, dlib loads its CNN models in a background thread (can take 30-90 seconds). The first scan after startup may still be slow. Subsequent scans are fast because encodings are cached.

**Bluetooth scan finds no devices**

- Make sure the machine running Flask has a Bluetooth adapter
- Confirm the user's BLE device MAC address is registered in DynamoDB
- Adjust `BT_RSSI_THRESHOLD` in `.env` to a lower value (e.g. `-70`) to increase range

**AWS services are unavailable**

Check that `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are set in `.env` and that the IAM user has permissions for DynamoDB, S3, Lambda, and IoT Core.

---

## Dependencies

| Package | Purpose |
|---|---|
| Flask 3.0.3 | Web framework |
| Flask-WTF 1.2.1 | CSRF protection and form handling |
| Werkzeug 3.1.8 | Password hashing, WSGI utilities |
| python-dotenv 1.0.0 | Load `.env` file into environment |
| waitress 2.1.2 | Production WSGI server (Windows) |
| bleak >= 0.21.1 | Bluetooth BLE scanning |
| face_recognition | Face encoding and comparison (dlib-based) |
| numpy | Array operations for face encoding |
| Pillow | Image loading and processing |
| boto3 >= 1.34.0 | AWS SDK (DynamoDB, S3, Lambda, IoT) |

---

## License

FYP Project - APU Semester VI
