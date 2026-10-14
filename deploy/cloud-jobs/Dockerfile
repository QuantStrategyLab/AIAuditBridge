# CI builds only. Deployment must use a reviewed digest, never a mutable tag.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
WORKDIR /app
COPY ops/cloud-jobs /app/ops/cloud-jobs
COPY ops/quant-monitor/quant_monitor_domain /app/ops/quant-monitor/quant_monitor_domain
COPY scripts /app/scripts
RUN useradd --uid 10001 --create-home monitor
USER 10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "ops/cloud-jobs/cloud_job.py"]
