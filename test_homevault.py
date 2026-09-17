import tempfile
import unittest
from pathlib import Path

from homevault import Vault


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.vault_dir = self.root / "vault"
        self.source.mkdir()
        (self.source / "a.txt").write_text("same content", encoding="utf-8")
        (self.source / "nested").mkdir()
        (self.source / "nested" / "b.txt").write_text("same content", encoding="utf-8")
        self.vault = Vault(self.vault_dir)

    def tearDown(self):
        self.temp.cleanup()

    def test_deduplicates_identical_files(self):
        result = self.vault.backup(self.source)
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["new_objects"], 1)
        self.assertEqual(len(list((self.vault_dir / "objects" / "sha256").rglob("*"))) - 1, 1)

    def test_restore_preserves_content(self):
        sid = str(self.vault.backup(self.source)["snapshot"])
        destination = self.root / "restored"
        count = self.vault.restore(sid, destination)
        self.assertEqual(count, 2)
        self.assertEqual((destination / "a.txt").read_text(encoding="utf-8"), "same content")
        self.assertEqual(
            (destination / "nested" / "b.txt").read_text(encoding="utf-8"), "same content"
        )

    def test_verify_detects_corruption(self):
        self.vault.backup(self.source)
        object_file = next(path for path in self.vault.objects.rglob("*") if path.is_file())
        object_file.write_bytes(b"tampered")
        result = self.vault.verify()
        self.assertEqual(result["corrupt"], 1)

    def test_refuses_vault_inside_source(self):
        with self.assertRaises(ValueError):
            Vault(self.source / "vault").backup(self.source)


if __name__ == "__main__":
    unittest.main()
