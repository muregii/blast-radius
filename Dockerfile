# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image the organisers labeled every commit in: keep it so the
# sqlglot tests we run behave the way theirs did.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# The exact environment the organisers labeled in.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# sqlglot at the pinned commit, straight from its own repository.
COPY buildtools/fetch_sqlglot.py /app/buildtools/fetch_sqlglot.py
RUN python /app/buildtools/fetch_sqlglot.py /repo
RUN python -m compileall -q /repo/sqlglot /repo/tests || true

# Build-time map: run the whole suite once against a recording copy of sqlglot
# (line coverage + comparison/and-or facts for silent-mutation proofs).
COPY tests.json /app/tests.json
COPY buildtools /app/buildtools
RUN python /app/buildtools/build_map.py /repo /app/tests.json /app/map.json 4

COPY selector.py silent.py rts_report.py /app/
RUN chmod -R a+rX /app /repo

CMD ["python", "/app/selector.py"]
