"""Tests factices. Un fichier FAIL à la racine simule une CI rouge."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import app  # noqa: E402

assert not os.path.exists(os.path.join(os.path.dirname(__file__), "..", "FAIL")), "FAIL présent : CI rouge simulée"
assert app.saluer("Ada") == "Bonjour Ada"
assert app.total([1, 2]) == 3
print("tests OK")
assert app.greet("Bob") == "Bonjour Bob"
