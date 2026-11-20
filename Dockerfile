FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libusb-1.0-0 \
    libusb-1.0-0-dev \
    bluez \
    dbus \
    libglib2.0-0 \
    network-manager \
    wireless-tools \
    iproute2 \
    i2c-tools \
    systemd \
    procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

EXPOSE 5000

CMD ["./start.sh"]
