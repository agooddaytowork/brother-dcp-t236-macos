"""Real eSCL HTTP requests with a simulated USB scan acquisition."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'src/scanner'))
import airscan_bridge as bridge

class FakeScanner:
    def __init__(self, **kwargs):
        pass
    def __enter__(self):
        return self
    def __exit__(self,*args):
        pass
    def scan(self,dpi,mode,cancel):
        image=Image.new('RGB',(2480,3504),(20,80,160))
        out=io.BytesIO()
        image.save(out,'JPEG',dpi=(300,300))
        yield out.getvalue()

def settings(width=2480,height=3508,mode='RGB24',fmt='image/png',dpi=300):
    return f'''<scan:ScanSettings {bridge.NS}><pwg:Version>2.0</pwg:Version>
    <scan:ScanRegions><scan:ScanRegion><pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
    <pwg:Width>{width}</pwg:Width><pwg:Height>{height}</pwg:Height><pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>
    </scan:ScanRegion></scan:ScanRegions><pwg:DocumentFormat>{fmt}</pwg:DocumentFormat>
    <pwg:InputSource>Platen</pwg:InputSource><scan:ColorMode>{mode}</scan:ColorMode>
    <scan:XResolution>{dpi}</scan:XResolution><scan:YResolution>{dpi}</scan:YResolution></scan:ScanSettings>'''.encode()

class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patches=[patch.object(bridge,'BASE',Path(self.tmp.name)),patch.object(bridge,'present',return_value=True),patch.object(bridge.t236scan,'T230',FakeScanner)]
        for p in self.patches:
            p.start();self.addCleanup(p.stop)
        bridge.JOBS.clear();bridge.ACTIVE=None
        self.server=bridge.ThreadingHTTPServer(('127.0.0.1',0),bridge.Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.close_server)

    def close_server(self):
        with bridge.LOCK:
            jobs=list(bridge.JOBS.values())
        for job in jobs:
            job['cancel'].set();job['done'].wait(5)
        self.server.shutdown();self.server.server_close();self.thread.join()

    def request(self,path,method='GET',body=None):
        request=urllib.request.Request(self.url+path,data=body,method=method,headers={'Content-Type':'text/xml'})
        return urllib.request.urlopen(request,timeout=10)

    def create(self,body=None):
        with self.request('/eSCL/ScanJobs','POST',body or settings()) as response:
            self.assertEqual(response.status,201)
            return response.headers['Location']

    def test_capabilities_and_idle_status(self):
        with self.request('/eSCL/ScannerCapabilities') as response:
            root=ET.fromstring(response.read())
            self.assertEqual(root.find(f'.//{{{bridge.SCAN}}}MaxHeight').text,'3508')
        with self.request('/eSCL/ScannerStatus') as response:
            self.assertEqual(ET.fromstring(response.read()).find(f'{{{bridge.PWG}}}State').text,'Idle')

    def test_full_a4_png_pads_bottom_without_scaling(self):
        location=self.create()
        with self.request(location+'/NextDocument') as response:
            self.assertEqual(response.headers['Content-Type'],'image/png')
            image=Image.open(io.BytesIO(response.read()))
            image.load()
        self.assertEqual(image.size,(2480,3508))
        self.assertEqual(image.getpixel((0,3507)),(255,255,255))
        self.assertLess(image.getpixel((0,0))[0],30)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(location+'/NextDocument')
        self.assertEqual(error.exception.code,404)
        error.exception.close()
        with self.request(location,'DELETE'):
            pass
        self.assertEqual(bridge.JOBS[location.rsplit('/',1)[-1]]['state'],'Completed')

    def test_crop_gray_jpeg(self):
        location=self.create(settings(120,160,'Grayscale8','image/jpeg'))
        with self.request(location+'/NextDocument') as response:
            image=Image.open(io.BytesIO(response.read()))
            image.load()
        self.assertEqual(image.size,(120,160))
        self.assertEqual(image.mode,'L')

    def test_unsupported_requests_rejected_before_usb(self):
        for body in [b'not XML',settings(5000,3508),settings(dpi=600),settings(fmt='application/pdf')]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.create(body)
            self.assertEqual(error.exception.code,400)
            error.exception.close()
        self.assertEqual(bridge.JOBS,{})

    def test_busy_and_cancellation_while_waiting_for_print_lock(self):
        import fcntl
        with open(Path(self.tmp.name)/'io.lock','a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            location=self.create()
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.create()
            self.assertEqual(error.exception.code,503)
            error.exception.close()
            with self.request(location,'DELETE'):
                pass
            job=bridge.JOBS[location.rsplit('/',1)[-1]]
            self.assertTrue(job['done'].wait(3))
            self.assertEqual(job['state'],'Canceled')
        self.assertIsNone(bridge.ACTIVE)

    def test_foreign_host_and_browser_origin_rejected(self):
        for headers in [{'Host':'scanner.attacker.example'}, {'Origin':'https://example.com'}]:
            request=urllib.request.Request(self.url+'/eSCL/ScannerCapabilities',headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request,timeout=5)
            self.assertEqual(error.exception.code,403)
            error.exception.close()

if __name__ == '__main__':
    unittest.main()
