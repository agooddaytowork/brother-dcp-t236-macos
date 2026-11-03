import importlib.util
from pathlib import Path
import plistlib
import unittest

ROOT=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location('manage',ROOT/'scripts/manage.py')
manage=importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)

class InstallTests(unittest.TestCase):
    def test_usb_selection_and_validation(self):
        uri,serial=manage.device_uri('usb://Brother/DCP-T236?serial=TEST-DEVICE')
        self.assertEqual(serial,'TEST-DEVICE')
        for uri in ['http://example.com','usb://Brother/DCP-T230?serial=X','usb://Brother/DCP-T236']:
            with self.assertRaises(RuntimeError):
                manage.device_uri(uri)

    def test_launchagents_serialize_paths_with_spaces_and_share_lock(self):
        agents=manage.service_plists(Path('/a path/venv/bin/python3'),'usb://Brother/DCP-T236?serial=TEST-DEVICE')
        self.assertEqual(len(agents),2)
        for label,data in agents.items():
            self.assertEqual(plistlib.loads(plistlib.dumps(data)),data)
            self.assertEqual(data['Label'],label)
            self.assertEqual(data['EnvironmentVariables']['BROTHER_IO_LOCK'],str(manage.BASE/'io.lock'))
            self.assertTrue(data['RunAtLoad'])
        printer=agents[manage.LABELS[0]]
        self.assertIn('localhost',printer['ProgramArguments'])
        self.assertIn('--no-web-forms',printer['ProgramArguments'])
        self.assertEqual(printer['ProgramArguments'][printer['ProgramArguments'].index('-r')+1],'off')

if __name__ == '__main__':
    unittest.main()
