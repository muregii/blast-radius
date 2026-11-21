"""Download sqlglot at the pinned commit from its own GitHub repository."""

import io
import shutil
import sys
import tarfile
import urllib.request

SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
dest = sys.argv[1] if len(sys.argv) > 1 else "/repo"
url = f"https://github.com/tobymao/sqlglot/archive/{SHA}.tar.gz"
data = urllib.request.urlopen(url, timeout=300).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    tar.extractall("/tmp/src", filter="data")
shutil.move(f"/tmp/src/sqlglot-{SHA}", dest)
shutil.rmtree("/tmp/src", ignore_errors=True)
print(f"sqlglot {SHA} -> {dest}")
