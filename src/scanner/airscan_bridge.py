#!/usr/bin/env python3
"""Local eSCL bridge for DCP-T236. GPL-2.0-or-later; full source supplied.

USB protocol: farafonoff/Brother-DCP-T230-MacOS-driver (t236scan.py).
This service binds loopback and advertises only to this Mac using Bonjour.
"""
import ctypes as C
import fcntl
import io
import json
from html import escape
import logging
import os
from pathlib import Path
import select
import signal
import socket
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from PIL import Image
import usb1
import t236scan

BASE = Path(os.environ.get('BROTHER_BASE_DIR', Path(__file__).resolve().parent.parent))
CONFIG = json.loads((BASE / 'config.json').read_text()) if (BASE / 'config.json').exists() else {}
SERIAL = CONFIG.get('serial', '')
PORT = 8632
DEVICE_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, 'brother-dcp-t236-usb:' + SERIAL))
NAME = 'Brother DCP-T236 Mac USB'
PWG = 'http://www.pwg.org/schemas/2010/12/sm'
SCAN = 'http://schemas.hp.com/imaging/escl/2011/05/03'
NS = f'xmlns:pwg="{PWG}" xmlns:scan="{SCAN}"'
LOCK = threading.RLock()
JOBS = {}
ACTIVE = None
STOP = threading.Event()

def xml_document(name, body):
    return f'<?xml version="1.0" encoding="UTF-8"?><scan:{name} {NS}>{body}</scan:{name}>'.encode()

CAPABILITIES = xml_document('ScannerCapabilities', '''
<pwg:Version>2.0</pwg:Version><pwg:MakeAndModel>Brother DCP-T236 Mac USB</pwg:MakeAndModel>
<pwg:SerialNumber>''' + escape(SERIAL) + '''</pwg:SerialNumber><scan:UUID>''' + DEVICE_UUID + '''</scan:UUID>
<scan:AdminURI>http://localhost:8632/eSCL/ScannerCapabilities</scan:AdminURI>
<scan:Platen><scan:PlatenInputCaps>
<scan:MinWidth>16</scan:MinWidth><scan:MinHeight>16</scan:MinHeight>
<scan:MaxWidth>2481</scan:MaxWidth><scan:MaxHeight>3508</scan:MaxHeight>
<scan:MaxPhysicalWidth>2481</scan:MaxPhysicalWidth><scan:MaxPhysicalHeight>3508</scan:MaxPhysicalHeight>
<scan:MaxScanRegions>1</scan:MaxScanRegions>
<scan:SettingProfiles><scan:SettingProfile name="0">
<scan:ColorModes><scan:ColorMode>RGB24</scan:ColorMode><scan:ColorMode>Grayscale8</scan:ColorMode></scan:ColorModes>
<scan:ColorSpaces><scan:ColorSpace>RGB</scan:ColorSpace></scan:ColorSpaces>
<scan:DocumentFormats><pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat><pwg:DocumentFormat>image/png</pwg:DocumentFormat>
<scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt><scan:DocumentFormatExt>image/png</scan:DocumentFormatExt></scan:DocumentFormats>
<scan:SupportedResolutions><scan:DiscreteResolutions><scan:DiscreteResolution>
<scan:XResolution>300</scan:XResolution><scan:YResolution>300</scan:YResolution>
</scan:DiscreteResolution></scan:DiscreteResolutions></scan:SupportedResolutions>
</scan:SettingProfile></scan:SettingProfiles>
<scan:SupportedIntents><scan:SupportedIntent>Document</scan:SupportedIntent><scan:SupportedIntent>Photo</scan:SupportedIntent><scan:SupportedIntent>Preview</scan:SupportedIntent></scan:SupportedIntents>
</scan:PlatenInputCaps></scan:Platen>''')

def present():
    try:
        with usb1.USBContext() as ctx:
            return any(d.getVendorID() == t236scan.VID and d.getProductID() == t236scan.PID for d in ctx.getDeviceIterator(skip_on_error=True))
    except Exception:
        return False

def scan_worker(job):
    global ACTIVE
    timer = threading.Timer(100, job['cancel'].set)
    timer.start()
    try:
        with open(BASE / 'io.lock', 'a+b') as lock:
            while True:
                if job['cancel'].is_set():
                    raise t236scan.Cancelled('Cancelled while waiting for printer')
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    job['cancel'].wait(.1)
            if not present():
                raise t236scan.DeviceNotFound('Turn on DCP-T236 and connect USB')
            # Always use the verified full-bed color USB request. Derive regions
            # and gray images locally so all advertised paths share this request.
            with t236scan.T230(serial_number=SERIAL or None) as scanner:
                data = t236scan.trim_to_jpeg(b''.join(scanner.scan(300, 'C24BIT', cancel=job['cancel'])))
        if job['cancel'].is_set():
            raise t236scan.Cancelled('Cancelled')
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.size != (2480, 3504):
                raise ValueError(f'Unexpected scan size {image.size}')
            region = job['region']
            if region[2] > 2480 or region[3] > 3504:
                canvas = Image.new('RGB', (2481, 3508), 'white')
                canvas.paste(image, (0, 0))
                image = canvas
            if region != (0, 0, 2480, 3504):
                image = image.crop(region)
            if job['mode'] == 'Grayscale8':
                image = image.convert('L')
            if region != (0, 0, 2480, 3504) or job['mode'] != 'RGB24' or job['format'] != 'image/jpeg':
                out = io.BytesIO()
                image.save(out, 'PNG' if job['format'] == 'image/png' else 'JPEG', dpi=(300, 300), quality=95)
                data = out.getvalue()
        with LOCK:
            job['data'] = data
            job['state'] = 'Completed'
        logging.info('Scan ready (%d bytes, %s)', len(data), job['format'])
    except t236scan.Cancelled:
        job['state'] = 'Canceled'
        logging.info('Scan canceled')
    except Exception:
        logging.exception('Scan failed')
        job['state'] = 'Aborted'
    finally:
        timer.cancel()
        with LOCK:
            ACTIVE = None
            job['done'].set()

def status():
    with LOCK:
        state = 'Processing' if ACTIVE else ('Idle' if present() else 'Stopped')
        body = f'<pwg:Version>2.0</pwg:Version><pwg:State>{state}</pwg:State><scan:Jobs>'
        for ident, job in list(JOBS.items()):
            age = int(time.monotonic() - job['created'])
            if age > 180 and job['done'].is_set():
                del JOBS[ident]
                continue
            body += f'<scan:JobInfo><pwg:JobUri>/eSCL/ScanJobs/{ident}</pwg:JobUri><pwg:JobUuid>{ident}</pwg:JobUuid><scan:Age>{age}</scan:Age><pwg:JobState>{job["state"]}</pwg:JobState><pwg:ImagesCompleted>{int(job["sent"])}</pwg:ImagesCompleted></scan:JobInfo>'
        return xml_document('ScannerStatus', body + '</scan:Jobs>')

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'BrotherT236AirScan/1.0'
    def parse_request(self):
        if not super().parse_request():
            return False
        # Native AirScan requests are same-machine clients, not web pages.
        # Reject DNS-rebinding Host names and browser-initiated operations.
        try:
            host = (urlsplit('http://' + self.headers.get('Host', '')).hostname or '').rstrip('.')
        except ValueError:
            host = ''
        if host not in ('localhost', '127.0.0.1', '::1') or self.headers.get('Origin'):
            self.reply(403)
            self.close_connection = True
            return False
        return True
    def setup(self):
        super().setup()
        self.connection.settimeout(120)
    def log_message(self, fmt, *args):
        if self.command == 'GET' and self.path.endswith('/ScannerStatus'):
            return
        logging.info('%s %s', self.client_address[0], fmt % args)
    def reply(self, code, data=b'', mime='text/xml', headers=None):
        self.send_response(code)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
    def do_HEAD(self):
        self.do_GET()
    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/')
        if path == '/eSCL/ScannerCapabilities':
            return self.reply(200, CAPABILITIES)
        if path == '/eSCL/ScannerStatus':
            return self.reply(200, status())
        if path.startswith('/eSCL/ScanJobs/'):
            ident = path.split('/')[3]
            with LOCK:
                job = JOBS.get(ident)
            if not job:
                return self.reply(404)
            if path.endswith('/NextDocument'):
                if not job['done'].wait(110):
                    return self.reply(503)
                with LOCK:
                    if job['state'] != 'Completed' or job['sent']:
                        return self.reply(404 if job['state'] != 'Aborted' else 503)
                    data = job.pop('data')
                    job['sent'] = True
                return self.reply(200, data, job['format'])
            return self.reply(200, status())
        self.reply(404)
    def do_POST(self):
        global ACTIVE
        if self.path.rstrip('/') != '/eSCL/ScanJobs':
            return self.reply(404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                return self.reply(413)
            raw = self.rfile.read(length)
            root = ET.fromstring(raw)
            values = {e.tag.rsplit('}', 1)[-1]: (e.text or '').strip() for e in root.iter()}
            logging.info('Scan settings: %s', values)
            if int(values.get('XResolution', 300)) != 300 or int(values.get('YResolution', 300)) != 300:
                return self.reply(400)
            mode = values.get('ColorMode', 'RGB24')
            fmt = values.get('DocumentFormatExt') or values.get('DocumentFormat', 'image/jpeg')
            if mode not in ('RGB24', 'Grayscale8') or fmt not in ('image/jpeg', 'image/png') or values.get('InputSource', 'Platen') != 'Platen':
                return self.reply(400)
            x, y = int(values.get('XOffset', 0)), int(values.get('YOffset', 0))
            w, h = int(values.get('Width', 2481)), int(values.get('Height', 3508))
            if x < 0 or y < 0 or w < 16 or h < 16 or x + w > 2481 or y + h > 3508:
                return self.reply(400)
        except (ValueError, ET.ParseError):
            return self.reply(400)
        with LOCK:
            if ACTIVE or len(JOBS) > 8:
                # Remove old complete jobs before accepting a new one.
                status()
                if ACTIVE or len(JOBS) > 8:
                    return self.reply(503)
            ident = str(uuid.uuid4())
            job = dict(created=time.monotonic(), done=threading.Event(), cancel=threading.Event(), state='Processing', sent=False, region=(x, y, x+w, y+h), mode=mode, format=fmt)
            JOBS[ident] = job
            ACTIVE = ident
            threading.Thread(target=scan_worker, args=(job,), daemon=True).start()
        self.reply(201, headers={'Location': f'/eSCL/ScanJobs/{ident}'})
    def do_DELETE(self):
        ident = self.path.rstrip('/').split('/')[-1]
        with LOCK:
            job = JOBS.get(ident)
            if not job:
                return self.reply(404)
            job['cancel'].set()
            if job['done'].is_set():
                job.pop('data', None)
                if not job['sent']:
                    job['state'] = 'Canceled'
        self.reply(200)

def advertise():
    """Use Apple's DNS-SD library, with local-only interface and localhost SRV."""
    lib = C.CDLL('/usr/lib/libSystem.B.dylib')
    ref = C.c_void_p()
    callback_type = C.CFUNCTYPE(None, C.c_void_p, C.c_uint32, C.c_int32, C.c_char_p, C.c_char_p, C.c_char_p, C.c_void_p)
    @callback_type
    def registered(ref, flags, error, name, regtype, domain, context):
        logging.info('Bonjour registration: error=%d name=%s', error, name)
    lib.DNSServiceRegister.argtypes = [C.POINTER(C.c_void_p), C.c_uint32, C.c_uint32, C.c_char_p, C.c_char_p, C.c_char_p, C.c_char_p, C.c_uint16, C.c_uint16, C.c_void_p, callback_type, C.c_void_p]
    entries = ['txtvers=1', 'vers=2.0', 'pdl=image/jpeg,image/png', 'ty='+NAME, 'note=USB', 'uuid='+DEVICE_UUID, 'rs=eSCL', 'cs=color,grayscale', 'is=platen', 'duplex=F']
    txt = b''.join(bytes([len(e.encode())]) + e.encode() for e in entries)
    error = lib.DNSServiceRegister(C.byref(ref), 0, 0xffffffff, NAME.encode(), b'_uscan._tcp', b'local.', b'localhost.', socket.htons(PORT), len(txt), txt, registered, None)
    if error:
        raise RuntimeError(f'Bonjour error {error}')
    lib.DNSServiceRefSockFD.argtypes = [C.c_void_p]
    lib.DNSServiceProcessResult.argtypes = [C.c_void_p]
    lib.DNSServiceRefDeallocate.argtypes = [C.c_void_p]
    try:
        fd = lib.DNSServiceRefSockFD(ref)
        while not STOP.is_set():
            if select.select([fd], [], [], 1)[0]:
                if lib.DNSServiceProcessResult(ref):
                    raise RuntimeError('Bonjour processing failed')
    finally:
        lib.DNSServiceRefDeallocate(ref)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.umask(0o077)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    server.daemon_threads = True
    def advertise_or_restart():
        try:
            advertise()
        except Exception:
            logging.exception('Bonjour failed; restarting service')
            os._exit(1)
    threading.Thread(target=advertise_or_restart, daemon=True).start()
    def shutdown(*_):
        STOP.set()
        with LOCK:
            for job in JOBS.values():
                job['cancel'].set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    logging.info('Local AirScan ready on port %d', PORT)
    server.serve_forever(.25)
    with LOCK:
        pending = [j['done'] for j in JOBS.values()]
    for event in pending:
        event.wait(70)
    server.server_close()

if __name__ == '__main__':
    main()
