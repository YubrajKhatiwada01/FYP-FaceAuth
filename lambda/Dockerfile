# FaceAuth Post-Auth Lambda — container image
# Base: AWS-provided Python 3.11 Lambda runtime
FROM public.ecr.aws/lambda/python:3.11

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Lambda handler
COPY lambda_function.py ${LAMBDA_TASK_ROOT}/

# Lambda entry point
CMD ["lambda_function.handler"]
