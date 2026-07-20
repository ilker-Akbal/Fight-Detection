FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONPATH=/app

WORKDIR /app

# TR university network için Ubuntu mirror (archive.ubuntu.com bloklu)
RUN echo "deb http://ftp.linux.org.tr/ubuntu/ jammy main restricted universe multiverse" > /etc/apt/sources.list \
 && echo "deb http://ftp.linux.org.tr/ubuntu/ jammy-updates main restricted universe multiverse" >> /etc/apt/sources.list \
 && echo "deb http://ftp.linux.org.tr/ubuntu/ jammy-backports main restricted universe multiverse" >> /etc/apt/sources.list \
 && echo "deb http://ftp.linux.org.tr/ubuntu/ jammy-security main restricted universe multiverse" >> /etc/apt/sources.list

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.10 \
    python3.10-dev \
    python3-pip \
    build-essential \
    gcc \
    git \
    curl \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libpq-dev \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3.10 /usr/bin/python && \
    ln -sf /usr/bin/pip3 /usr/bin/pip

RUN pip install --upgrade pip setuptools wheel

# CUDA 12.8 PyTorch kurulumu
RUN pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128

COPY requirements.txt /app/requirements.txt

# requirements içindeki torch satırları varsa çakışmasın diye burada sorun çıkarsa onları dosyadan kaldıracağız.
RUN pip install -r /app/requirements.txt

COPY . /app

WORKDIR /app/Fight_backend_project/backend_frontend_project

EXPOSE 8000

CMD ["bash", "-lc", "python manage.py migrate && python manage.py collectstatic --noinput || true && gunicorn backend_frontend_project.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 180"]