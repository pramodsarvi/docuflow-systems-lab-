FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/tmp/hf PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY docuflow ./docuflow
COPY evals ./evals
# Run as an unprivileged user; /tmp is the only writable path (HF cache), so the root filesystem can be read-only.
RUN useradd --uid 10001 --no-create-home docuflow
USER 10001
CMD ["python", "-m", "docuflow", "up"]
