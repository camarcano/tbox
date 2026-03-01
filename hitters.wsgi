#!/usr/bin/env python3
import sys
import os

sys.path.insert(0, '/var/www/hitters')
os.environ.setdefault('FLASK_ENV', 'production')

from app import app as application

if __name__ == '__main__':
    application.run()
