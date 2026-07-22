#!/bin/sh
set -e
python /code/manage.py collectstatic --noinput
python /code/manage.py migrate --noinput
python /code/manage.py createcachetable
