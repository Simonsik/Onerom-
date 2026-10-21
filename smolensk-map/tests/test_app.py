import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch
import app


class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / 'traffic.sqlite3'
        app.initialize_database(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_import_and_filter(self):
        with sqlite3.connect(self.path) as db:
            self.assertEqual(len(app.bootstrap(db)['sensors']), 18)
            self.assertEqual(app.detector_counts(db, 0, '00:00', '23:59')['total'], 602)
            self.assertEqual(app.detector_counts(db, 0, '00:00', '23:59')['vehicles'], 127)
            self.assertEqual(app.detector_counts(db, 1, '00:00', '23:59')['total'], 1313)
            self.assertEqual(app.detector_counts(db, 1, '00:00', '23:59')['vehicles'], 200)
            result = app.detector_counts(db, 0, '06:00', '06:00')
            self.assertEqual(result, {'counts': {'D17': 1}, 'total': 1, 'vehicles': 1})
            self.assertEqual(app.detector_counts(db, 0, '00:00', '01:00')['total'], 0)
            for dataset, start, end in [(0,'09:00','07:00'), (0,'24:00','25:00'), (0,'bad','12:00'), (99,'00:00','23:59')]:
                with self.assertRaises(ValueError):
                    app.detector_counts(db, dataset, start, end)

    def test_reimport_is_idempotent(self):
        app.initialize_database(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM passages').fetchone()[0], 1915)

    def test_route_validation(self):
        self.assertEqual(app.route_points('32,54;33,55'), '32.0,54.0;33.0,55.0')
        for value in ['', 'nan,54;32,55', '181,54;32,55', '32,91;32,55', 'https://example.com', '32,54,7;32,55']:
            with self.assertRaises(ValueError):
                app.route_points(value)

    def test_http(self):
        server = app.make_server('127.0.0.1', 0, self.path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base + '/') as response:
                self.assertIn('text/html', response.headers['Content-Type'])
                self.assertIn(b'/script.js', response.read().replace(b'"script.js"',b'"/script.js"'))
            with urlopen(base + '/api/bootstrap') as response:
                self.assertEqual(len(json.load(response)['datasets']), 2)
            with urlopen(base + '/api/detectors?dataset=1') as response:
                self.assertEqual(json.load(response)['total'], 1313)
            for path, code in [('/data/traffic.sqlite3',404),('/api/detectors?dataset=99',400),('/api/route?points=bad',400)]:
                with self.assertRaises(HTTPError) as raised:
                    urlopen(base + path)
                self.assertEqual(raised.exception.code, code)
            with patch('app.urllib.request.urlopen', side_effect=OSError('offline')):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(base + '/api/route?points=32,54;33,55')
                self.assertEqual(raised.exception.code, 502)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
