# A plain stdlib-only Python server (the benchmark of compute/README.md): at start it imports a realistic set of modules
# and runs random-access queries over a SQLite file of a few MB that is made at build time, before it listens.
FROM python:3.14.10-slim@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999
WORKDIR /app
COPY make_db.py app.py ./
RUN python make_db.py /app/data.db && rm make_db.py && ls -l /app/data.db
ENV PORT=8080
CMD ["python", "app.py"]
