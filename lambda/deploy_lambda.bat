@echo off
REM ============================================================
REM  deploy_lambda.bat - Deploy faceauth-post-auth-trigger
REM  Run this from inside the lambda\ folder
REM ============================================================

setlocal

REM --- Load IOT_ENDPOINT from parent .env ----------------------
for /f "tokens=1,2 delims==" %%A in ('findstr /i "IOT_ENDPOINT" ..\.env') do (
    if /i "%%A"=="IOT_ENDPOINT" set IOT_ENDPOINT=%%B
)

REM --- Settings ------------------------------------------------
set FUNCTION_NAME=faceauth-post-auth-trigger
set REGION=us-east-1
set RUNTIME=python3.11
set HANDLER=lambda_function.handler
set TIMEOUT=30
set MEMORY=256

REM --- You must create this role first in AWS Console ----------
REM --- then paste its ARN below --------------------------------
set ROLE_ARN=arn:aws:iam::454492134332:role/faceauth-lambda-role

echo.
echo ============================================================
echo  FaceAuth Lambda Deploy
echo  Function : %FUNCTION_NAME%
echo  Region   : %REGION%
echo  Endpoint : %IOT_ENDPOINT%
echo ============================================================
echo.

REM --- Package -------------------------------------------------
echo [1/3] Packaging lambda_function.py...
if exist lambda_package.zip del lambda_package.zip
powershell -command "Compress-Archive -Path lambda_function.py -DestinationPath lambda_package.zip -Force"
echo       Done.

REM --- Check if function already exists ------------------------
echo [2/3] Checking if function exists in AWS...
aws lambda get-function --function-name %FUNCTION_NAME% --region %REGION% >nul 2>&1

if %errorlevel%==0 (
    echo       Function exists -- updating code...
    aws lambda update-function-code ^
        --function-name %FUNCTION_NAME% ^
        --zip-file fileb://lambda_package.zip ^
        --region %REGION%

    echo       Updating environment variables...
    aws lambda update-function-configuration ^
        --function-name %FUNCTION_NAME% ^
        --region %REGION% ^
        --environment "Variables={DYNAMO_LOGS_TABLE=faceauth-logs,IOT_ENDPOINT=%IOT_ENDPOINT%,SNS_ALERT_ARN=}"
) else (
    echo       Function not found -- creating...
    aws lambda create-function ^
        --function-name %FUNCTION_NAME% ^
        --runtime %RUNTIME% ^
        --role %ROLE_ARN% ^
        --handler %HANDLER% ^
        --zip-file fileb://lambda_package.zip ^
        --timeout %TIMEOUT% ^
        --memory-size %MEMORY% ^
        --region %REGION% ^
        --environment "Variables={DYNAMO_LOGS_TABLE=faceauth-logs,IOT_ENDPOINT=%IOT_ENDPOINT%,SNS_ALERT_ARN=}"
)

echo.
echo [3/3] Verifying deployment...
aws lambda get-function --function-name %FUNCTION_NAME% --region %REGION% --query "Configuration.[FunctionName,Runtime,LastModified,State]" --output table

echo.
echo ============================================================
echo  Done! Check CloudWatch Logs after the next face scan.
echo  Log group: /aws/lambda/%FUNCTION_NAME%
echo ============================================================
endlocal
