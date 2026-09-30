FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OSDS_MEDIA_ROOT=/var/lib/osds/media

# The UID is pinned. The media volume is shared by osds-app and osds-worker and
# its ownership is fixed when Docker first populates it from this image, so a
# UID that drifted between releases would leave an upgraded install unable to
# write its own uploads.
RUN groupadd --gid 10001 osds \
 && useradd --uid 10001 --gid osds --no-create-home --shell /usr/sbin/nologin osds

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# settings.py raises at import when its secrets are unset, so collectstatic is
# given throwaway values for this one RUN. They are not ENV: nothing baked into
# the image can stand in for a real secret, and collectstatic never connects to
# the database.
RUN DJANGO_SECRET_KEY=build-only OSDS_SECRET_KEY=build-only \
    DATABASE_URL=postgresql://build@localhost/build \
    python manage.py collectstatic --noinput \
 && chmod 755 docker/entrypoint.sh \
 && mkdir -p "$OSDS_MEDIA_ROOT" \
 && chown osds:osds "$OSDS_MEDIA_ROOT"

USER osds
EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["app"]
