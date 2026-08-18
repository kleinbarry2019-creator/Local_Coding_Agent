from pathlib import Path


def register_filesystem_tools(registry, handlers):
    registry.register(
        name="filesystem.list",
        description="List files inside the controlled runtime workspace",
        risk="low",
        handler=handlers["list_files"],
        max_calls=10,
    )

    registry.register(
        name="filesystem.read",
        description="Read a file from the controlled runtime workspace",
        risk="low",
        handler=handlers["read_file"],
        max_calls=20,
    )

    registry.register(
        name="filesystem.write",
        description="Write a file inside the controlled runtime workspace",
        risk="medium",
        handler=handlers["write_file"],
        max_calls=10,
    )
