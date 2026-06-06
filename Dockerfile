# python:3.12-slim tracks Debian "bookworm", which ships Taskwarrior 2.6.2.
# (If you bump this to a 3.13/3.14 image it will be Debian "trixie" with
#  Taskwarrior 3.x. The app talks to `task` only via CLI + JSON export/import,
#  which is stable across 2.6 -> 3.x, so it works on either.)
FROM python:3.12-slim

# Install Taskwarrior
RUN apt-get update && \
    apt-get install -y --no-install-recommends taskwarrior && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application
COPY app.py .
COPY templates/ templates/

# Data directory for Taskwarrior storage. A bind-mount may shadow this at
# runtime; the app writes its own .taskrc on startup if one is not present.
RUN mkdir -p /data/.task && \
    printf 'data.location=/data/.task\nconfirmation=no\nverbose=nothing\nrecurrence=on\ngc=on\njson.array=on\n' > /data/.task/.taskrc

VOLUME ["/data"]
EXPOSE 62304

ENV TASKDATA=/data/.task \
    TASKRC=/data/.task/.taskrc \
    FLASK_ENV=production

CMD ["gunicorn", "--bind", "0.0.0.0:62304", "--workers", "2", "--timeout", "30", "app:app"]
