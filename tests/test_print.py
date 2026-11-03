"""Offline print regressions: real CoreGraphics output, no USB transfer."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT/'build/brother-t236-print'

def pdf(path, width=595.2756, height=841.8898, pages=1):
    # A one-inch red square at a one-inch offset makes physical scaling
    # measurable independently of the production drawing transform.
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'']
    kids = []
    for _ in range(pages):
        page_id = len(objects)+1
        stream_id = page_id+1
        kids.append(f'{page_id} 0 R')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << >> /Contents {stream_id} 0 R >>'.encode())
        content = b'1 0 0 rg 72 72 72 72 re f\n'
        objects.append(f'<< /Length {len(content)} >>\nstream\n'.encode()+content+b'endstream')
    objects[1] = f'<< /Type /Pages /Count {pages} /Kids [{" ".join(kids)}] >>'.encode()
    data = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f'{n} 0 obj\n'.encode()+obj+b'\nendobj\n')
    xref = len(data)
    data.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    path.write_bytes(data)

class PrintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.input = self.path/'input.pdf'
        self.out = self.path/'output.ppm'

    def invoke(self, mode='--preview', env=None):
        return subprocess.run([HELPER, mode, self.input, self.out], capture_output=True, env={**os.environ, **(env or {})})

    def square_bounds(self, width=595.2756, height=841.8898, env=None):
        pdf(self.input, width, height)
        result = self.invoke(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        with Image.open(self.out) as image:
            self.assertEqual(image.size, (2480, 3507))
            diff = ImageChops.difference(image, Image.new('RGB',image.size,'white'))
            bbox = diff.getbbox()
            self.assertIsNotNone(bbox)
            return bbox, image.getpixel(((bbox[0]+bbox[2])//2, (bbox[1]+bbox[3])//2))

    def test_one_inch_a4_is_300_pixels(self):
        bbox, pixel = self.square_bounds()
        self.assertLessEqual(abs(bbox[2]-bbox[0]-300), 2)
        self.assertLessEqual(abs(bbox[3]-bbox[1]-300), 2)
        self.assertLessEqual(abs(bbox[0]-300), 2)
        self.assertEqual(pixel, (255,0,0))

    def test_landscape_preserves_scale(self):
        bbox, _ = self.square_bounds(841.8898,595.2756)
        self.assertLessEqual(abs(bbox[2]-bbox[0]-300), 2)
        self.assertLessEqual(abs(bbox[3]-bbox[1]-300), 2)

    def test_letter_fits_a4_width(self):
        bbox, _ = self.square_bounds(612,792)
        self.assertLessEqual(abs(bbox[2]-bbox[0]-292), 2)

    def test_grayscale_channels_match(self):
        _, pixel = self.square_bounds(env={'IPP_PRINT_COLOR_MODE':'monochrome'})
        self.assertEqual(pixel[0], pixel[1])
        self.assertEqual(pixel[1], pixel[2])
        self.assertGreater(pixel[0], 0)
        self.assertLess(pixel[0], 255)

    def test_ranges_and_copies(self):
        pdf(self.input,pages=3)
        result = self.invoke('--convert', {'IPP_PAGE_RANGES':'2-3','IPP_COPIES':'2'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count(b'INFO: Prepared page'), 4)
        data = self.out.read_bytes()
        self.assertIn(b'@PJL ENTER LANGUAGE=PWGRASTER',data)
        self.assertIn(b'RaS2',data)
        self.assertTrue(data.endswith(b'\x1b%-12345X'))

    def test_invalid_document_and_options_fail(self):
        self.input.write_text('This is not a PDF')
        self.assertNotEqual(self.invoke().returncode, 0)
        pdf(self.input)
        for env in [{'IPP_PAGE_RANGES':'4'}, {'IPP_PAGE_RANGES':'3-2'}, {'IPP_COPIES':'0'}, {'IPP_COPIES':'100'}]:
            self.out.unlink(missing_ok=True)
            self.assertNotEqual(self.invoke('--convert',env).returncode, 0)

    def test_existing_output_is_not_overwritten(self):
        pdf(self.input)
        self.out.write_bytes(b'keep me')
        self.assertNotEqual(self.invoke().returncode,0)
        self.assertEqual(self.out.read_bytes(),b'keep me')

if __name__ == '__main__':
    unittest.main()
