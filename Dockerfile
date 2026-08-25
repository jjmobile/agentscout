# AgentScout — hardened observer/publisher for technocore.chat. No compilers, no curl, no shell tools in the image.
FROM python:3.12-slim

RUN groupadd --gid 10001 agentscout \
 && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin --no-create-home agentscout \
 && mkdir -p /data && chown 10001:10001 /data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir --no-compile . \
 && rm -rf /root/.cache

USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AGENTSCOUT_DB=/data/agentscout.db
VOLUME ["/data"]

# Internal, zero-network health check: the process must have written the DB recently.
HEALTHCHECK --interval=120s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import os,sys,time;p=os.environ['AGENTSCOUT_DB'];sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<900 else 1)"]

ENTRYPOINT ["agentscout"]
