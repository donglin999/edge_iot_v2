#!/bin/bash

nohup python /app/run.py > /dev/null 2>&1 &
#nohup python /app/data_upload.py > /dev/null 2>&1 &


sleep 500000