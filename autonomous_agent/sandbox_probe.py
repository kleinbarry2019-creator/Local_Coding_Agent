import os
from pathlib import Path

print("=== SANDBOX PROBE ===")
print("cwd:", os.getcwd())
print("uid:", os.getuid())
print("pid:", os.getpid())

checks = [
    Path("/var/home/mklein"),
    Path("/etc/shadow"),
    Path("/proc/1"),
    Path("/sys"),
]

for path in checks:
    try:
        exists = path.exists()
        print(f"{path}: exists={exists}")
    except Exception as exc:
        print(f"{path}: ERROR {type(exc).__name__}: {exc}")

print("workspace files:")
for item in sorted(Path(".").iterdir()):
    print(" ", item.name)

print("SANDBOX PROBE OK")
