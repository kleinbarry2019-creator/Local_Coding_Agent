from pathlib import Path

print("=== SANDBOX ESCAPE TEST ===")

targets = [
    Path("/var/home/mklein/ESCAPE_TEST"),
    Path("/etc/ESCAPE_TEST"),
    Path("/tmp/ESCAPE_TEST"),
    Path("/var/ESCAPE_TEST"),
    Path("/workspace/ESCAPE_TEST"),
]

for target in targets:
    try:
        target.write_text("MUST NOT EXIST", encoding="utf-8")
        print("WRITE SUCCEEDED:", target)
    except Exception as exc:
        print(
            "WRITE BLOCKED:",
            target,
            type(exc).__name__,
            str(exc),
        )

print()
print("=== VISIBILITY ===")

for target in [
    Path("/var/home/mklein"),
    Path("/etc/shadow"),
    Path("/sys"),
    Path("/proc/1"),
    Path("/workspace"),
]:
    print(f"{target}: exists={target.exists()}")

print()
print("=== SANDBOX ESCAPE TEST END ===")
