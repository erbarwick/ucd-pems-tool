"""
settings.py
-----------
This module provides package constants.
By: Sebastian D. Goodfellow, Ph.D.
"""

# Import 3rd party libraries
import os
from pathlib import Path

# ── PATH CONFIGURATION ───────────────────────────────────────────────────────
# Dynamically locate the base directory (parent of the 'pems' package folder)
PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGE_DIR.parent

# Set data directory
DATA_PATH = str(BASE_DIR / 'data')

# Set base url
BASE_URL = 'https://pems.dot.ca.gov'

# Clearing house url
CLEARING_HOUSE_URL = '{}/?srq=clearinghouse&district_id={}&yy={}&type={}&returnformat=text'

# Set available districts
DISTRICTS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12']
