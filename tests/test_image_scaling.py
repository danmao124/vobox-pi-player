"""Real mpv/libavfilter regression; runs without taking over the display.

Requires mpv and ffmpeg on PATH. The separate live-Pi test also checks vo=gpu.
"""
import hashlib
import json
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playback_report as report


def write_png(path, width, height):
    def chunk(name, data):
        return (struct.pack('>I', len(data)) + name + data
                + struct.pack('>I', zlib.crc32(name + data)))
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    pixels = (b'\0' + b'\x20\x80\xc0' * width) * height
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
                     + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


@unittest.skipUnless(shutil.which('mpv') and shutil.which('ffmpeg'),
                     'real mpv and ffmpeg required')
class ImageScalingIntegrationTests(unittest.TestCase):
    def test_dimensions_originals_and_image_video_transitions(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sock_path = str(root / 'mpv.sock')
            playlist = root / 'main.txt'
            playlist.write_text('fixture\n')
            video = root / 'video.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=blue:s=320x240:r=24:d=2', '-c:v', 'mpeg4', str(video)],
                           check=True, capture_output=True, timeout=15)
            with (root / 'mpv.log').open('w+') as log:
                process = subprocess.Popen([
                    'mpv', '--no-config', '--vo=null', '--ao=null', '--hwdec=auto',
                    '--idle=yes', '--keep-open=always', '--image-display-duration=inf',
                    '--input-terminal=no', f'--input-ipc-server={sock_path}',
                ], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 5
                    while not Path(sock_path).exists():
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail('mpv failed to start')
                        time.sleep(0.02)

                    def get(prop):
                        with socket.socket(socket.AF_UNIX) as sock:
                            sock.settimeout(3)
                            sock.connect(sock_path)
                            sock.sendall((json.dumps({'command': ['get_property', prop],
                                                     'request_id': 1}) + '\n').encode())
                            with sock.makefile('r') as stream:
                                for line in stream:
                                    reply = json.loads(line)
                                    if reply.get('request_id') == 1:
                                        self.assertEqual(reply['error'], 'success')
                                        return reply['data']
                        self.fail('mpv disconnected')

                    original_hwdec = get('options/hwdec')
                    cases = [(3375, 4219, 3072, 3840), (4219, 3375, 3840, 3072),
                             (1080, 1920, 1080, 1920), (3840, 2160, 3840, 2160)]
                    for w, h, out_w, out_h in cases:
                        with self.subTest(width=w, height=h):
                            image = root / f'{w}x{h}.png'
                            write_png(image, w, h)
                            digest = hashlib.sha256(image.read_bytes()).digest()
                            report.load_and_record(sock_path, str(image), playlist, 1,
                                                   root / 'history.json', 'image')
                            params = get('video-out-params')
                            self.assertEqual((params['w'], params['h']), (out_w, out_h))
                            # Integer output dimensions can round by one pixel.
                            self.assertAlmostEqual(params['aspect'], w / h,
                                                   delta=1 / min(out_w, out_h))
                            self.assertEqual(hashlib.sha256(image.read_bytes()).digest(), digest)
                            self.assertTrue(get('vf'))
                            report.load_and_record(sock_path, str(video), playlist, 2,
                                                   root / 'history.json', 'video')
                            self.assertEqual(get('vf'), [])
                            self.assertEqual(get('options/hwdec'), original_hwdec)
                            params = get('video-out-params')
                            self.assertEqual((params['w'], params['h']), (320, 240))
                finally:
                    process.terminate()
                    process.wait(timeout=5)
                    log.seek(0)
                    self.assertNotIn('Option no not found', log.read())


if __name__ == '__main__':
    unittest.main()
