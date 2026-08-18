import json
import os
from datetime import datetime


class MemoryManager:

    def __init__(self, storage_path="agent_memory.json"):
        self.storage_path = storage_path
        self.state = {}

        self.load()


    def store(self, key, value):
        """
        Speichert einen Wert dauerhaft im Agenten-Gedächtnis.
        """

        self.state[key] = {
            "value": value,
            "timestamp": datetime.now().isoformat()
        }

        self.save()

        return {
            "stored": True,
            "key": key,
            "value": value,
        }


    def retrieve(self, key):
        """
        Liest einen gespeicherten Wert aus.
        """

        entry = self.state.get(key)

        if entry is None:
            return None

        return entry["value"]


    def get_all(self):
        """
        Gibt den kompletten Speicher zurück.
        """

        return self.state


    def delete(self, key):
        """
        Entfernt einen Speicher-Eintrag.
        """

        if key in self.state:
            del self.state[key]
            self.save()

            return {
                "deleted": True,
                "key": key
            }

        return {
            "deleted": False,
            "key": key
        }


    def clear(self):
        """
        Löscht den kompletten Speicher.
        """

        self.state = {}
        self.save()

        return {
            "cleared": True
        }


    def save(self):
        """
        Persistiert den Speicher auf die Festplatte.
        """

        try:
            with open(
                self.storage_path,
                "w",
                encoding="utf-8"
            ) as file:
                json.dump(
                    self.state,
                    file,
                    indent=4,
                    ensure_ascii=False
                )

        except Exception as error:
            print(
                f"Memory save error: {error}"
            )


    def load(self):
        """
        Lädt vorhandenes Agenten-Gedächtnis.
        """

        if not os.path.exists(self.storage_path):
            self.state = {}
            return

        try:
            with open(
                self.storage_path,
                "r",
                encoding="utf-8"
            ) as file:
                self.state = json.load(file)

        except Exception as error:
            print(
                f"Memory load error: {error}"
            )

            self.state = {}
