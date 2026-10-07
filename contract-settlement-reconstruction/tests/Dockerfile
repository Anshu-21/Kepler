FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends util-linux && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest==9.1.1 pytest-json-ctrf==0.5.2
WORKDIR /tests
COPY . /tests/
RUN python /tests/build_check.py
RUN mkdir -p /app /logs/verifier /sandbox && cp /tests/support/*.py /app/ && cp /tests/candidate_runner.py /app/candidate_runner.py && chmod 755 /app && chmod 644 /app/*.py && chmod 700 /logs/verifier && chmod 711 /sandbox && chmod +x /tests/test.sh
CMD ["/tests/test.sh"]
