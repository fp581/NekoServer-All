#!/usr/bin/env python3
"""Fail LAN gateway startup on mismatched storage or an absent LAN address."""
import argparse,importlib.util,json
from pathlib import Path
import network_access
from neko_common import safe_path

def verify(config):
    spec=importlib.util.spec_from_file_location('gateway_mount_guard',Path(__file__).with_name('storage-guard.py'));mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);mod.verify(config);network_access.verify_lan(config)
def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);a=p.parse_args();verify(json.loads(safe_path(a.config).read_text()))
if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Gateway startup guard failed; inspect configured drives and LAN address locally')
