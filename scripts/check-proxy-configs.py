#!/usr/bin/env python3
"""Check that derived gateway configs match proxy/config.example.yaml.

Outside GATEWAY-SPECIFIC blocks, each derived config must equal the template
except for the airport node and documented placeholder values.
"""
import re
import sys
from pathlib import Path

PROXY = Path(__file__).resolve().parents[1] / "proxy"
TEMPLATE = PROXY / "config.example.yaml"
DERIVED = ["config.yaml", "config-california.yaml"]
BLOCK = re.compile(r"[ ]*# ==== GATEWAY-SPECIFIC.*?# ==== END GATEWAY-SPECIFIC ====\n", re.S)
AIRPORT = re.compile(r"  - name: airport\n.*?\n\n", re.S)
# Template placeholder -> pattern accepted for the real value.
PLACEHOLDER_IPS = re.compile(r"IP-CIDR,[0-9.]+/32,DIRECT,no-resolve\n(?=  # Local LAN subnet)")


def normalize(text):
    text = text[text.index("mode: rule\n"):]
    text = BLOCK.sub("<GATEWAY-SPECIFIC>\n", text)
    text = AIRPORT.sub("<AIRPORT>\n\n", text)
    return PLACEHOLDER_IPS.sub("<DIRECT-HOST>\n", text)


def main():
    expected = normalize(TEMPLATE.read_text()).splitlines()
    failed = False
    for name in DERIVED:
        path = PROXY / name
        if not path.exists():
            print(f"skip  {name} (not present)")
            continue
        actual = normalize(path.read_text()).splitlines()
        if actual == expected:
            print(f"ok    {name}")
            continue
        failed = True
        print(f"DIFF  {name} differs from config.example.yaml outside GATEWAY-SPECIFIC blocks:")
        import difflib
        for line in difflib.unified_diff(expected, actual, "config.example.yaml", name, lineterm="", n=1):
            # Never print credential-looking lines.
            if re.search(r"(password|username|uuid|public-key|short-id|cipher)\s*:", line):
                line = line[0] + "  <credential line hidden>"
            print("      " + line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
