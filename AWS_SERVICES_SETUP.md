# AWS Cloud Services and Setup Guide

This document details all AWS Cloud services utilized in the FaceAuth system, their architecture, and the complete step-by-step setup process.

---

## 1. Overview of AWS Services Used

The FaceAuth system uses a hybrid edge-cloud architecture with five core AWS services:

```
+-------------------------------------------------------------------------------+
|                             AWS CLOUD ARCHITECTURE                            |
|                                                                               |
|  +-------------------+   +--------------------+   +-----------------------+   |
|  |   Amazon S3       |   |  Amazon DynamoDB   |   |     AWS IoT Core      |   |
|  | (Photo Storage)   |   | (Users & Logs DB)  |   | (MQTT Door Controller)|   |
|  +---------+---------+   +---------+----------+   +-----------+-----------+   |
|            ^                       ^                          |               |
|            |                       |                          v               |
|  +---------+-----------------------+----------+   +-----------+-----------+   |
|  |               Flask Backend                |   |   ESP32 Microcontroller|  |
|  |     (Bluetooth Scanner + Face Match)       |   |       (Door Servo)    |   |
|  +---------------------+----------------------+   +-----------------------+   |
|                        |                                                      |
|                        v                                                      |
|  +---------------------+----------------------+                               |
|  |             AWS Lambda Function            |                               |
|  |         (Post-Auth Event Trigger)          |                               |
|  +--------------------------------------------+                               |
+-------------------------------------------------------------------------------+
```

### 1.1 Amazon DynamoDB (NoSQL Database)
DynamoDB acts as the primary data store for user records, physical access points, and access audit logs.

* **`faceauth-users` Table**:
  * **Partition Key**: `id` (String / UUID)
  * **Stored Data**: `username`, `full_name`, `email`, `role`, `status`, `bluetooth_mac`, `photo_path` (S3 key), `photos` (array of sample keys), `created_at`.
* **`faceauth-logs` Table**:
  * **Partition Key**: `id` (String / UUID)
  * **Stored Data**: `timestamp`, `event_type` (`Access Granted` / `Access Denied`), `username`, `access_point`, `status`, `details`.
* **`faceauth-access-points` Table**:
  * **Partition Key**: `id` (String / UUID)
  * **Stored Data**: `name`, `location`, `type`, `status`, `last_used`.

### 1.2 Amazon S3 (Simple Storage Service)
S3 is used for secure and scalable object storage of enrolled face photos.
* **Bucket Name**: `faceauth-fyp` (or custom name defined in `.env`).
* **Storage Structure**: `photos/{user_id}_{timestamp}.jpg`.
* **Security**: Private bucket; photos are retrieved directly by the server via IAM credentials and memory-cached for fast subsequent verification.

### 1.3 AWS IoT Core (MQTT Broker)
AWS IoT Core provides low-latency messaging between the FaceAuth Flask application and physical access hardware.
* **Topic**: `server_room/door_command`
* **QoS**: 1 (At least once delivery)
* **Payload**: `"ACCESS_GRANTED"`
* **Target Device**: ESP32 microcontroller with SG90/MG996R servo motor.

### 1.4 AWS Lambda (Serverless Event Handler)
Lambda executes asynchronous post-authentication processing without slowing down the user verification flow.
* **Function Name**: `faceauth-post-auth-trigger`
* **Runtime**: Python 3.10 / 3.11 / 3.12
* **Trigger**: Invoked asynchronously (`Event` invocation type) by Flask upon each face verification attempt.
* **Responsibilities**:
  * Publishes audit metrics to IoT monitoring topics (`faceauth/auth/granted` or `faceauth/auth/denied`).
  * Processes security alerts and notifications.

### 1.5 AWS IAM (Identity and Access Management)
IAM manages secure access controls, API keys, and execution roles for the Flask server and Lambda functions.

---

## 2. Step-by-Step AWS Setup Guide

Follow these steps to configure your AWS account for the FaceAuth project.

### Step 1: Create an IAM User for the Application
1. Log in to the **AWS Management Console**.
2. Navigate to **IAM** > **Users** > **Create user**.
3. Set the username (e.g., `faceauth-app-user`).
4. Attach the following managed policies or create an inline policy:
   * `AmazonDynamoDBFullAccess` (or custom policy restricted to `faceauth-*` tables)
   * `AmazonS3FullAccess` (or custom policy restricted to your bucket)
   * `AWSIoTFullAccess` (or custom policy restricted to `server_room/door_command`)
   * `AWSLambda_FullAccess` (or custom policy allowing `lambda:InvokeFunction`)
5. Go to the **Security credentials** tab of the created user.
6. Click **Create access key** > Choose **Application running outside AWS**.
7. Save the **Access Key ID** and **Secret Access Key** securely.

---

### Step 2: Create the Amazon S3 Bucket
1. Navigate to **Amazon S3** > **Create bucket**.
2. Set the **Bucket name** (e.g., `faceauth-fyp`).
3. Select your preferred **AWS Region** (e.g., `us-east-1` or `ap-south-1`).
4. Keep **Block all public access** enabled (photos are accessed privately through server credentials).
5. Click **Create bucket**.

---

### Step 3: Create DynamoDB Tables
Navigate to **Amazon DynamoDB** > **Tables** > **Create table** and create the three tables:

#### Table 1: Users
* **Table name**: `faceauth-users`
* **Partition key**: `id` (String)
* **Table class**: DynamoDB Standard
* **Capacity mode**: On-demand

#### Table 2: Access Logs
* **Table name**: `faceauth-logs`
* **Partition key**: `id` (String)
* **Table class**: DynamoDB Standard
* **Capacity mode**: On-demand

#### Table 3: Access Points
* **Table name**: `faceauth-access-points`
* **Partition key**: `id` (String)
* **Table class**: DynamoDB Standard
* **Capacity mode**: On-demand

---

### Step 4: Configure AWS IoT Core for ESP32 Door Control
1. Navigate to **AWS IoT Core**.
2. Under **Settings** (left sidebar), find your **Device data endpoint** (format: `xxxxxxxxxxxxxx-ats.iot.<region>.amazonaws.com`). Copy this endpoint.
3. Under **Manage** > **All devices** > **Things**, click **Create things** > **Create single thing** (e.g., `FaceAuth_ESP32_Door`).
4. Under **Security** > **Policies**, create a policy (e.g., `FaceAuth_ESP32_Policy`):
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": ["iot:Connect"],
         "Resource": ["arn:aws:iot:*:*:client/*"]
       },
       {
         "Effect": "Allow",
         "Action": ["iot:Subscribe"],
         "Resource": ["arn:aws:iot:*:*:topicfilter/server_room/door_command"]
       },
       {
         "Effect": "Allow",
         "Action": ["iot:Receive"],
         "Resource": ["arn:aws:iot:*:*:topic/server_room/door_command"]
       }
     ]
   }
   ```
5. Download the device certificate, private key, and Amazon Root CA 1 to flash into the ESP32 code.

---

### Step 5: Deploy the AWS Lambda Function
1. Open terminal in the project directory:
   ```bash
   cd lambda
   deploy_lambda.bat
   ```
   *Alternatively, create a ZIP file containing `lambda_function.py` and upload it directly to AWS Lambda console.*
2. Set Function Configuration:
   * **Function name**: `faceauth-post-auth-trigger`
   * **Runtime**: Python 3.11 or Python 3.12
   * **Handler**: `lambda_function.lambda_handler`
3. Under **Configuration** > **Permissions**, ensure the execution role has permissions to publish to IoT topics.

---

### Step 6: Configure the Project Environment File (`.env`)
Create or edit the `.env` file in the root of the project:

```env
# Flask Security
SECRET_KEY=your_random_secret_key_here
FLASK_ENV=production

# AWS Credentials
AWS_REGION=ap-south-1
AWS_ACCESS_KEY_ID=AKIAXXXXXXXXXXXXXXXX
AWS_SECRET_ACCESS_KEY=YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY

# AWS Resources
S3_BUCKET_NAME=faceauth-fyp
DYNAMO_USERS_TABLE=faceauth-users
DYNAMO_LOGS_TABLE=faceauth-logs
DYNAMO_POINTS_TABLE=faceauth-access-points
LAMBDA_FUNCTION_NAME=faceauth-post-auth-trigger
IOT_ENDPOINT=xxxxxxxxxxxxxx-ats.iot.ap-south-1.amazonaws.com

# Recognition & Bluetooth Settings
FACE_TOLERANCE=0.55
BT_RSSI_THRESHOLD=-85
```

---

### Step 7: Verify and Run the System
1. Install project dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Run the application:
   ```bash
   python app.py
   ```
3. Open `http://127.0.0.1:5000` in your web browser.
4. If configured correctly, user enrollments, facial scans, S3 photo storage, and DynamoDB logs will sync with AWS Cloud in real-time.
