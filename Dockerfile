# syntax=docker/dockerfile:1
# One image for every role: api, workers (io / vlm / embed), the model simulator and the gc CronJob.
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt requirements-eval.txt ./
# WITH_EVAL=1 adds RAGAS + LangChain for worker-ragas (~300 MB). Build with WITH_EVAL=0 for a slimmer image without it.
ARG WITH_EVAL=1
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt \
    && if [ "$WITH_EVAL" = "1" ]; then /opt/venv/bin/pip install -r requirements-eval.txt; fi

FROM python:3.12-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="docuflow" org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/pramodsarvi/docuflow-systems-lab-"
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/tmp/hf DOCUFLOW_VERSION=${VERSION}
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY docuflow ./docuflow
COPY evals ./evals
# Unprivileged user; /tmp is the only writable path (HF cache), so the root filesystem can be read-only.
RUN useradd --uid 10001 --no-create-home docuflow
USER 10001
EXPOSE 8791 9100 11434
CMD ["python", "-m", "docuflow", "up", "--host", "0.0.0.0"]
