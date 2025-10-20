#!/bin/bash
# Restart Django and Celery services

echo "Stopping existing services..."
pkill -f "python3.*manage.py runserver" 2>/dev/null
pkill -f "celery.*worker" 2>/dev/null
sleep 3

echo "Starting Django..."
PYTHONPATH=/mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/backend \
DJANGO_SETTINGS_MODULE=control_plane.settings \
python3 /mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/backend/manage.py runserver \
> /tmp/django.log 2>&1 &

echo "Waiting for Django to start..."
sleep 5

echo "Starting Celery..."
cd /mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/backend && \
DJANGO_SETTINGS_MODULE=control_plane.settings \
celery -A control_plane worker -l info --pool=solo \
> /tmp/celery.log 2>&1 &

echo "Waiting for Celery to start..."
sleep 3

echo ""
echo "Services started!"
echo "Django log: tail -f /tmp/django.log"
echo "Celery log: tail -f /tmp/celery.log"
echo ""
pgrep -af "manage.py runserver|celery.*worker" | head -2
