import os
import platform
import time

print("Agent-Test gestartet")
print("cwd:", os.getcwd())
print("python:", platform.python_version())

for i in range(5):
    print(f"Schritt {i + 1}/5")
    time.sleep(1)

print("SAFE TEST OK")
