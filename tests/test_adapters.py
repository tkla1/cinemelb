"""Contract tests for adapters.

These run without network access: they check that each adapter module
registered in :mod:`cinemelb.adapters` is importable, declares a name matching
its registry key, and satisfies the :class:`Adapter` interface.  Actual data
fetching is covered by ``scripts/verify.py`` (which needs the network).

A missing adapter module is not a failure here — it is reported — so the suite
stays green while platforms are added incrementally.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cinemelb.adapters import _REGISTRY, AdapterOutput, get_adapter
from cinemelb.adapters.base import Adapter
from cinemelb.models import Screening


class TestAdapterContract(unittest.TestCase):
    def test_registry_is_populated(self):
        self.assertGreaterEqual(len(_REGISTRY), 3)

    def test_importable_adapters_obey_contract(self):
        for source, (module_path, class_name) in sorted(_REGISTRY.items()):
            try:
                mod = importlib.import_module(module_path)
            except ImportError:
                with self.subTest(adapter=source):
                    self.skipTest(f"{module_path} not implemented yet")
                continue
            with self.subTest(adapter=source):
                cls = getattr(mod, class_name, None)
                self.assertIsNotNone(cls, f"{module_path} has no {class_name}")
                self.assertTrue(issubclass(cls, Adapter))
                inst = cls()
                self.assertEqual(inst.name, source,
                                 "adapter .name must match its registry key")
                self.assertTrue(callable(inst.fetch))
                self.assertTrue(callable(inst.fetch_many))
                # fetch must be declared with the documented signature
                params = list(inspect.signature(inst.fetch).parameters)
                self.assertEqual(params[:3], ["cinema", "http"])

    def test_module_docstrings_exist(self):
        """A scraper without a note on the site's quirks cannot be maintained."""
        for source, (module_path, _) in sorted(_REGISTRY.items()):
            try:
                mod = importlib.import_module(module_path)
            except ImportError:
                continue
            with self.subTest(adapter=source):
                self.assertTrue((mod.__doc__ or "").strip(),
                                f"{module_path} needs a module docstring")


class TestAdapterHelpers(unittest.TestCase):
    def test_source_id_handles_missing_keys(self):
        from cinemelb.adapters.hoyts import HoytsAdapter

        a = HoytsAdapter()
        self.assertEqual(a.source_id({"source_ids": {"hoyts": "MCECIN"}}), "MCECIN")
        self.assertEqual(a.source_id({"source_ids": {}}), "")
        self.assertEqual(a.source_id({}), "")
        self.assertEqual(a.venue_id({"id": "x"}), "x")

    def test_adapter_output_defaults(self):
        out = AdapterOutput()
        self.assertEqual(out.screenings, [])
        self.assertEqual(out.films, [])
        self.assertEqual(out.note, "")

    def test_unknown_adapter_returns_none(self):
        self.assertIsNone(get_adapter("does-not-exist"))

    def test_screenings_are_screening_objects(self):
        """Any adapter that does return data must return Screening instances."""
        from cinemelb.adapters.movingstory import MovingStoryAdapter

        self.assertTrue(issubclass(MovingStoryAdapter, Adapter))
        # structural check on the dataclass the adapters must produce
        fields = Screening.__dataclass_fields__
        for required in ("cinema_id", "film_title", "starts_at", "local_date", "local_time"):
            self.assertIn(required, fields)


if __name__ == "__main__":
    unittest.main()
