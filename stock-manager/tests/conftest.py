"""The checks that guard a release (licence, file signature) are off for the suite, and tested on their own."""

import os

os.environ.setdefault("STOCK_LICENSE", "off")
os.environ.setdefault("STOCK_INTEGRITY", "off")
os.environ.setdefault("STOCK_VAULT_KEYRING", "0")
