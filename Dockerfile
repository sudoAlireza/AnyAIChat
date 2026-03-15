# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /usr/src/app

# Copy the requirements file into the container at /usr/src/app
COPY requirements.txt ./

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Create data directory
RUN mkdir -p /usr/src/app/data

# Copy the rest of the application's code into the container at /usr/src/app
COPY . .

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python healthcheck.py || exit 1

# Run main.py when the container launches
CMD ["python", "main.py"]
