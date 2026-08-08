"""One module, one copy.

app.py is started as a script, which makes it the module "__main__". Everything
under backend/ reaches back for globals with a lazy `import app as _app` — and
with nothing registered under "app", that import used to execute the file a
second time as a separate module object. Two DB connections, two LOCKs guarding
different objects, two LATEST dicts (the blueprints read one, half the samplers
wrote the other), and two of every worker thread: the release build SSH-probed
each remote twice per interval and wrote duplicate rows into every append-only
table.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_APP_SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "app.py"), encoding="utf-8").read()


class TestSingleModuleInstance(unittest.TestCase):
    """app.py is started as a script, so it is the module "__main__". Everything
    under backend/ reaches back for globals with a lazy `import app as _app` —
    which, with nothing registered under "app", executed this file a second time
    as a separate module: two DB connections, two LOCKs guarding different
    objects, two LATEST dicts, and two of every worker thread. The release build
    was SSH-probing each remote twice per interval and writing duplicate rows
    into every append-only table.
    """

    def test_main_registers_itself_under_its_import_name(self):
        """The guard must alias __main__ into sys.modules['app'] — and must use
        setdefault, so importing app normally (as the tests do) never clobbers the
        real module with __main__."""
        src = _APP_SRC
        self.assertIn('sys.modules.setdefault("app", sys.modules["__main__"])', src)

    def test_alias_precedes_the_first_backend_import(self):
        """Ordering is the whole fix: a backend import that runs before the alias
        is registered would trigger the second execution anyway."""
        src = _APP_SRC
        alias = src.index('sys.modules.setdefault("app"')
        first_backend = min(src.index("\nfrom backend."), src.index("\nimport backend.")
                            if "\nimport backend." in src else len(src))
        self.assertLess(alias, first_backend,
                        "the sys.modules alias must come before any backend import")

    def test_workers_start_once_per_process(self):
        """The thread-start block sits at module level, so a second execution of
        the module is what duplicated every worker."""
        src = _APP_SRC
        self.assertEqual(src.count("threading.Thread(target=collector"), 1)
        self.assertEqual(src.count("threading.Thread(target=fast_sampler"), 1)
        self.assertEqual(src.count("threading.Thread(target=host_poller"), 1)


if __name__ == "__main__":
    unittest.main()
