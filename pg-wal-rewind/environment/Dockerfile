FROM python:3.13-slim-bookworm
WORKDIR /app
COPY app/ /app/
RUN chmod -R a+rX /app && chmod -R a-w /app/datasets /app/reference /app/INCIDENT.md
