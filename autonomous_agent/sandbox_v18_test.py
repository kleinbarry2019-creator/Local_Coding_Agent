from pathlib import Path
import os

print("=== V18 HARDENING TEST ===")

checks = [
    Path("/workspace/write_test"),
    Path("/proc/1"),
    Path("/dev/null"),
    Path("/etc/shadow"),
    Path("/sys"),
]

for p in checks:
    try:
        if p == Path("/workspace/write_test"):
            p.write_text("TEST")
            print("WRITE OK:", p)
        else:
            print(
                "VISIBLE:",
                p,
                p.exists()
            )

    except Exception as exc:
        print(
            "BLOCKED:",
            p,
            type(exc).__name__,
        )


print()
print("UID:", os.getuid())
print("PID:", os.getpid())

print("=== V18 END ===")
