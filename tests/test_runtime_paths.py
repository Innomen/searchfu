"""Pinned deployment parity; no real database access."""
import json,os,runpy,subprocess,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch
import runtime_paths

SOURCE=Path(__file__).resolve().parents[1]
ADAPTER=Path(os.environ['SEARCHFU_EMS_ADAPTER']) if os.environ.get('SEARCHFU_EMS_ADAPTER') else None

class InstalledPathsTests(unittest.TestCase):
    def test_generated_launcher_ignores_inherited_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);home=root/'synthetic-user';app=root/'app';app.mkdir()
            (app/'searchfu.sh').write_text('python3 - <<\'PYTHON\'\nimport json,os\nprint(json.dumps({k:os.environ[k] for k in '+repr(list(runtime_paths.pinned_paths(home)))+'}))\nPYTHON\n')
            launcher=root/'searchfu';launcher.write_text(runtime_paths.launcher_text(app,root/'env',home))
            env={**os.environ,**{k:str(root/'unused-backup') for k in runtime_paths.pinned_paths(home)}}
            output=subprocess.check_output(['/bin/bash',str(launcher),'--help'],env=env,text=True)
            self.assertEqual(json.loads(output),runtime_paths.pinned_paths(home))
    def test_installed_launcher_matches_generator_and_public_environment(self):
        launcher=Path.home()/'.local/bin/searchfu'
        if not os.environ.get('SEARCHFU_CHECK_INSTALLED') or not launcher.exists():self.skipTest('set SEARCHFU_CHECK_INSTALLED=1 for deployment parity')
        self.assertEqual(launcher.read_text(),runtime_paths.launcher_text(SOURCE,Path.home()/'.local/share/searchfu-env'))
        with tempfile.TemporaryDirectory() as tmp:
            fake=Path(tmp)/'bash'
            # Intercept only the dispatched shell, before application/index access.
            fake.write_text('#!/usr/bin/python3\nimport json,os,sys\nsys.path.insert(0,'+repr(str(SOURCE))+')\nfrom runtime_paths import pinned_paths\nprint(json.dumps({"all_index_locations_pinned":all(os.environ.get(k)==v for k,v in pinned_paths().items())}))\n')
            fake.chmod(0o755)
            env={**os.environ,'PATH':tmp+':'+os.environ['PATH'],**{k:'/synthetic-unused-backup' for k in runtime_paths.pinned_paths()}}
            out=subprocess.check_output(['/bin/bash',str(launcher),'--help'],env=env,text=True)
            self.assertTrue(json.loads(out)['all_index_locations_pinned'])
    def test_missing_primary_never_uses_available_backup(self):
        import search
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);backup=root/'backup.sqlite3'
            c=search.connect(backup);c.close()
            launcher=root/'searchfu'
            launcher.write_text(runtime_paths.launcher_text(SOURCE,Path(sys.prefix),root/'synthetic-user'))
            env={**os.environ,'SEARCHFU_DB':str(backup),'SEARCHFU_ANN_DB':str(backup)}
            result=subprocess.run(['/bin/bash',str(launcher),'stream','synthetic','--fts'],env=env,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn('"stage": "done"',result.stdout)
            self.assertFalse(Path(runtime_paths.pinned_paths(root/'synthetic-user')['SEARCHFU_DB']).exists())

    def test_ems_adapter_uses_same_pinned_family(self):
        if ADAPTER is None or not ADAPTER.is_file():self.skipTest('set SEARCHFU_EMS_ADAPTER for integration parity')
        fake=types.ModuleType('sentence_transformers')
        with patch.dict(sys.modules,{'sentence_transformers':fake}),patch.dict(os.environ,{k:'/synthetic-unused-backup' for k in runtime_paths.pinned_paths()}):
            adapter=runpy.run_path(str(ADAPTER))
            self.assertEqual(str(adapter['DB']),runtime_paths.pinned_paths()['SEARCHFU_DB'])
            adapter['runtime']()
            self.assertTrue(all(os.environ[k]==v for k,v in runtime_paths.pinned_paths().items()))

if __name__=='__main__':unittest.main()
