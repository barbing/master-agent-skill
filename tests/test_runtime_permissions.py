import os
from pathlib import Path
import sys
import tempfile
import unittest

from master_agent_runtime.contracts import ContractError
from master_agent_runtime.permissions import executable_read_roots,filesystem_profile

from runtime_fixtures import fake_executable, symlinks_available


class PermissionConstructionTests(unittest.TestCase):
    def test_secondary_python_gets_readonly_runtime_libraries_without_broad_parent_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            # POSIX keeps an interpreter's libraries under the prefix above
            # bin/; a Windows install keeps them beside python.exe.
            root=Path(tmp);prefix=root/'env'
            binary=fake_executable(prefix/('python3.10' if os.name=='nt' else 'bin/python3.10'))
            profile=filesystem_profile(root/'work',(),writable=True,executables=(str(binary),))
            self.assertEqual(profile['filesystem'][str(prefix.resolve())],'read')
            self.assertNotIn(str(root.resolve()),profile['filesystem'])

    @unittest.skipUnless(symlinks_available(),'this account cannot create symlinks')
    def test_directory_symlink_in_launcher_chain_is_admitted_without_home_wide_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);release=root/'package'/'versions'/'one'/'bin'
            binary=fake_executable(release/'tool')
            (root/'package'/'current').symlink_to(release.parent,target_is_directory=True)
            launcher=root/'launcher';launcher.mkdir()
            (launcher/binary.name).symlink_to(root/'package'/'current'/'bin'/binary.name)
            reads=executable_read_roots(launcher/binary.name)
            self.assertIn(str((root/'package').resolve()),reads)
            self.assertIn(str(release.resolve()),reads)
            self.assertNotIn(str(Path.home()),reads)
            self.assertNotIn('/',reads)

    @unittest.skipUnless(sys.platform=='darwin','macOS platform scratch policy')
    def test_future_system_temporary_files_are_denied_and_ambient_workspace_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile=filesystem_profile(Path(tmp)/'work',(),writable=True)
            for root in ('/tmp','/private/tmp','/var/tmp','/private/var/tmp'):
                self.assertEqual(profile['filesystem'][root+'/**'],'deny')
        with self.assertRaises(ContractError):
            filesystem_profile('/private/tmp/unsafe-runtime',(),writable=True)

    def test_executable_read_does_not_replace_equal_control_denial(self):
        executable=Path(sys.executable).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            profile=filesystem_profile(Path(tmp)/'work',(),writable=True,denied=(executable.parent,))
            self.assertEqual(profile['filesystem'][str(executable.parent)],'deny')
