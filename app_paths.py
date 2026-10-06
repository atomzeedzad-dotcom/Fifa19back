"""Paths shared by source launches and the portable Windows application."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


def runtime_root() -> Path:
    override = os.environ.get('FIFA19_LOCAL_RUNTIME')
    if override:
        return Path(override).resolve()
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'FIFA19LocalFUT'


VERSION = '0.2.1'
