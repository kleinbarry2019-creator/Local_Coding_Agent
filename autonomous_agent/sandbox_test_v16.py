from pathlib import Path
import os

print("cwd:", os.getcwd())
print("HOME:", Path("/var/home/mklein").exists())
print("SHADOW:", Path("/etc/shadow").exists())
print("SYS:", Path("/sys").exists())
print("PROC1:", Path("/proc/1").exists())
print("WORKSPACE:", Path("/workspace").exists())
