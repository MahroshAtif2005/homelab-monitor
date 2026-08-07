"""Unit tests for container disk attribution — `_norm_src` / `_covers` /
`_shared_mount_sources`.

The case that prompted these: on a toolbox/distrobox host, one stopped container
mounted `/srv/models` while five running ones mounted `/` at `/run/host`. Nobody
mounted the *same string*, so the shared-source filter (string equality) let the
whole 1.25 TB models tree be billed to the stopped container.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


def _ct(cid, *mounts, size_rw=0):
    """A /containers/json?size=1 row. `mounts` are (source, rw) or (source, rw, type)."""
    return {
        "Id": cid,
        "SizeRw": size_rw,
        "Mounts": [
            {"Source": m[0], "RW": m[1], "Type": (m[2] if len(m) > 2 else "bind")}
            for m in mounts
        ],
    }


class TestNormSrc(unittest.TestCase):
    def test_relative_and_empty_sources_are_rejected(self):
        # podman reports these for pseudo-mounts; _container_disk always skipped them
        for src in ("devpts", "tmpfs", "", None):
            self.assertIsNone(app._norm_src(src))

    def test_normalises_dot_dot_and_trailing_slash(self):
        self.assertEqual(app._norm_src("/srv/models/"), "/srv/models")
        self.assertEqual(app._norm_src("/srv/models/../cache"), "/srv/cache")
        self.assertEqual(app._norm_src("/srv/./models"), "/srv/models")

    def test_collapses_slashes_including_the_posix_double(self):
        self.assertEqual(app._norm_src("/srv//models"), "/srv/models")
        # os.path.normpath keeps a leading "//" verbatim — two spellings of one path
        self.assertEqual(app._norm_src("//srv/models"), "/srv/models")

    def test_root_stays_root(self):
        self.assertEqual(app._norm_src("/"), "/")


class TestCovers(unittest.TestCase):
    def test_strict_ancestor(self):
        self.assertTrue(app._covers("/srv", "/srv/models"))
        self.assertTrue(app._covers("/", "/srv/models"))

    def test_equality_is_not_coverage(self):
        """Same-source sharing is the caller's other branch, not this one."""
        self.assertFalse(app._covers("/srv/models", "/srv/models"))
        self.assertFalse(app._covers("/", "/"))

    def test_not_a_sibling_sharing_a_prefix(self):
        # the string-prefix trap: /srv must not "contain" /srvfoo
        self.assertFalse(app._covers("/srv", "/srvfoo"))
        self.assertFalse(app._covers("/srv/models", "/srv/models-old"))
        # compose named volumes with a common prefix must not collide either
        self.assertFalse(app._covers("/var/lib/docker/volumes/p_db/_data",
                                     "/var/lib/docker/volumes/p_db_data/_data"))

    def test_child_does_not_cover_its_parent(self):
        self.assertFalse(app._covers("/srv/models", "/srv"))


class TestSharedMountSources(unittest.TestCase):
    def setUp(self):
        app._shared_note["seen"] = frozenset()   # the NOTE line is printed on change

    def test_equal_sources_still_shared(self):
        """The original behaviour must survive: same path, two containers."""
        sized = [_ct("a", ("/srv/share", True)), _ct("b", ("/srv/share", True))]
        self.assertEqual(app._shared_mount_sources(sized), frozenset({"/srv/share"}))

    def test_sole_owner_is_not_shared(self):
        """A container with its own volume keeps being charged for it."""
        sized = [_ct("a", ("/var/lib/app", True)), _ct("b", ("/var/lib/other", True))]
        self.assertEqual(app._shared_mount_sources(sized), frozenset())

    def test_nested_under_another_containers_mount(self):
        """The reported bug: nobody mounts the same string, yet the data is shared."""
        sized = [
            _ct("stopped-toolbox", ("/srv/models", True)),
            _ct("running-toolbox", ("/", True)),
        ]
        self.assertEqual(app._shared_mount_sources(sized), frozenset({"/srv/models"}))

    def test_the_parent_owner_keeps_its_data(self):
        """One-directional on purpose: a container mounting a subdirectory of mine
        must not make my exclusive data disappear from the report."""
        sized = [
            _ct("media", ("/srv/media", True)),          # 500 GB, mostly exclusive
            _ct("gallery", ("/srv/media/photos", True)),  # small shared subdir
        ]
        self.assertEqual(app._shared_mount_sources(sized),
                         frozenset({"/srv/media/photos"}))

    def test_three_containers_merge(self):
        """Two whole-root toolboxes plus the nested one: still exactly one verdict."""
        sized = [
            _ct("stopped", ("/srv/models", True)),
            _ct("tool1", ("/", True)),
            _ct("tool2", ("/", True)),
        ]
        self.assertEqual(app._shared_mount_sources(sized), frozenset({"/srv/models", "/"}))

    def test_read_only_mounts_do_not_make_data_shared(self):
        """Our own /rootfs is read-only: it must not turn everyone else's volume
        into shared data, or every row would collapse to SizeRw."""
        sized = [_ct("app", ("/var/lib/app", True)), _ct("monitor", ("/", False))]
        self.assertEqual(app._shared_mount_sources(sized), frozenset())

    def test_non_bind_volume_types_ignored(self):
        sized = [_ct("a", ("/srv/models", True)), _ct("b", ("/", True, "tmpfs"))]
        self.assertEqual(app._shared_mount_sources(sized), frozenset())

    def test_docker_named_volumes_are_handled_like_binds(self):
        sized = [
            _ct("a", ("/var/lib/docker/volumes/data/_data", True, "volume")),
            _ct("b", ("/var/lib/docker/volumes/data/_data", True, "volume")),
        ]
        self.assertEqual(app._shared_mount_sources(sized),
                         frozenset({"/var/lib/docker/volumes/data/_data"}))

    def test_same_container_mounting_parent_and_child_is_not_shared(self):
        """One container owning both /srv and /srv/models shares with nobody."""
        sized = [_ct("solo", ("/srv", True), ("/srv/models", True))]
        self.assertEqual(app._shared_mount_sources(sized), frozenset())

    def test_same_container_mounting_one_source_twice(self):
        sized = [_ct("solo", ("/srv/data", True), ("/srv/data", True))]
        self.assertEqual(app._shared_mount_sources(sized), frozenset())

    def test_relative_sources_never_reach_the_shared_set(self):
        """Consistent with _container_disk, which can't measure them anyway."""
        sized = [_ct("a", ("devpts", True)), _ct("b", ("devpts", True))]
        self.assertEqual(app._shared_mount_sources(sized), frozenset())

    def test_sharing_is_seen_through_unnormalised_spellings(self):
        sized = [_ct("a", ("/srv/models", True)), _ct("b", ("/srv/../srv/models/", True))]
        self.assertEqual(app._shared_mount_sources(sized), frozenset({"/srv/models"}))

    def test_dot_dot_that_escapes_is_not_a_child(self):
        """/srv/models/../cache normalises to /srv/cache — not under /srv/models."""
        sized = [_ct("a", ("/srv/models", True)), _ct("b", ("/srv/models/../cache", True))]
        self.assertEqual(app._shared_mount_sources(sized), frozenset())


class TestContainerDiskUsesShared(unittest.TestCase):
    SIZES = {"/srv/models": 1_251_228_588_133, "/srv/media": 500_000_000_000}

    def setUp(self):
        app._shared_note["seen"] = frozenset()
        self._real_dir_size = app._dir_size
        app._dir_size = lambda path, timeout=120: self.SIZES.get(path, 0)

    def tearDown(self):
        app._dir_size = self._real_dir_size

    def test_nested_shared_mount_is_not_billed(self):
        """End to end over the helpers: the stopped container is left with its
        writable layer instead of the whole models tree."""
        stopped = _ct("stopped-toolbox", ("/srv/models", True), size_rw=40_887)
        running = _ct("running-toolbox", ("/", True), size_rw=3_070_000)
        shared = app._shared_mount_sources([stopped, running])
        self.assertEqual(app._container_disk(stopped, None, shared), 40_887)

    def test_without_the_shared_set_it_is_billed(self):
        """Control: same container, no sharing information, keeps the old (wrong)
        total — so the test above measures the filter, not luck."""
        stopped = _ct("stopped-toolbox", ("/srv/models", True), size_rw=40_887)
        self.assertEqual(app._container_disk(stopped, None, frozenset()),
                         40_887 + 1_251_228_588_133)

    def test_parent_owner_still_billed_when_a_child_is_shared(self):
        """The one-directional rule, end to end: /srv/media stays on its owner."""
        media = _ct("media", ("/srv/media", True), size_rw=100)
        gallery = _ct("gallery", ("/srv/media/photos", True), size_rw=50)
        shared = app._shared_mount_sources([media, gallery])
        self.assertEqual(app._container_disk(media, None, shared), 100 + 500_000_000_000)
        self.assertEqual(app._container_disk(gallery, None, shared), 50)

    def test_unnormalised_source_still_skipped(self):
        """`src in shared` compares normalised paths on both sides — if only one
        side were normalised the skip would silently miss."""
        stopped = _ct("stopped", ("/srv/./models/", True), size_rw=7)
        running = _ct("tool", ("/", True))
        shared = app._shared_mount_sources([stopped, running])
        self.assertEqual(app._container_disk(stopped, None, shared), 7)


if __name__ == "__main__":
    unittest.main()
