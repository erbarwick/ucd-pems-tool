"""
handler.py
----------
PeMS Handler using requests library
"""

import os
import re
import sys
import json
import time
import logging
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ── CONFIGURATION ───────────────────────────────────────────────────────
BASE_URL = 'https://pems.dot.ca.gov'
DATA_PATH = os.getenv('PEMS_DATA_PATH', './data')

# ── LOGGER SETUP ───────────────────────────────────────────────────────
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s [%(levelname)-8s] %(message)s')
handler.setFormatter(formatter)
LOGGER.addHandler(handler)

class PeMSHandler:
    def __init__(self, username=None, password=None, debug=False):
        self.username = username or os.getenv('PEMS_USERNAME')
        self.password = password or os.getenv('PEMS_PASSWORD')
        self.debug = debug
        self.session = requests.Session()
        self._setup_session()
        self._login()
        self.label_reference, self.form_data = None, None

    def _setup_session(self):
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        self.session.verify = False  # ⚠️ Only for testing. Use proper certs in production.
        requests.packages.urllib3.disable_warnings()

    def _login(self):
        LOGGER.info('Requesting initial page...')
        resp = self.session.get(BASE_URL)
        resp.raise_for_status()
        LOGGER.info('Initial page opened.')

        soup = BeautifulSoup(resp.text, 'html.parser')
        form = soup.find('form')
        if not form:
            raise Exception('Login form not found')

        action = form.get('action', '/')
        if not action.startswith('http'):
            action = f"{BASE_URL}{action}" if not action.startswith('/') else f"{BASE_URL}{action}"

        LOGGER.info('Logging in...')
        
        # Include 'redirect' field as required by PeMS
        login_data = {
            'redirect': '',
            'username': self.username,
            'password': self.password,
            'login': 'Login'
        }

        resp = self.session.post(action, data=login_data, allow_redirects=True)
        resp.raise_for_status()

        # ✅ Check for successful login markers
        if 'Logout' not in resp.text and 'logout' not in resp.text.lower():
            if self.debug:
                with open('debug_login_failure.html', 'w', encoding='utf-8') as f:
                    f.write(resp.text)
            raise Exception('Login failed - no logout link found')

        LOGGER.info('Logged in successfully.')
        self.last_response = resp


    def _get_html_object(self):
        return BeautifulSoup(self.last_response.text, 'html.parser')

    def download_file(self, file_name, file_url, save_path=None):
        try:
            save_path = save_path or DATA_PATH
            os.makedirs(save_path, exist_ok=True)
            full_url = file_url if file_url.startswith('http') else f"{BASE_URL}{file_url}"
            LOGGER.info(f'Start download: {file_name}')
            resp = self.session.get(full_url, stream=True)
            resp.raise_for_status()
            file_path = os.path.join(save_path, file_name)
            with open(file_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            LOGGER.info(f'Download completed: {file_path}')
            return True
        except Exception as e:
            LOGGER.error(f'Error downloading {file_name}: {e}')
            return False

    def get_text_content(self, url_path):
        try:
            full_url = url_path if url_path.startswith('http') else f"{BASE_URL}{url_path}"
            resp = self.session.get(full_url)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            LOGGER.error(f'Error fetching content: {e}')
            return None