import json
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import urlopen
from unittest.mock import patch
import app


class ApplicationTests(unittest.TestCase):
    """Проверяет импорт, расчёты и HTTP API на временной базе данных."""
    @classmethod
    def setUpClass(cls):
        """Создаёт временный каталог и импортирует таблицы в общую базу тестов."""
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / 'traffic.sqlite3'
        app.initialize_database(cls.path)

    @classmethod
    def tearDownClass(cls):
        """Удаляет временную базу и каталог после завершения класса тестов."""
        cls.temp.cleanup()

    def test_import_and_filter(self):
        """Проверяет число детекторов, проездов и машин в обеих выборках.

        Проверяет включение границ минуты, пустой интервал и отклонение
        неверного времени, обратного интервала и неизвестной выборки.
        """
        with sqlite3.connect(self.path) as db:
            self.assertEqual(len(app.bootstrap(db)['sensors']), 18)
            self.assertEqual(app.detector_counts(db, 0, '00:00', '23:59')['total'], 3002)
            self.assertEqual(app.detector_counts(db, 0, '00:00', '23:59')['vehicles'], 1327)
            self.assertEqual(app.detector_counts(db, 1, '00:00', '23:59')['total'], 3713)
            self.assertEqual(app.detector_counts(db, 1, '00:00', '23:59')['vehicles'], 1400)
            result = app.detector_counts(db, 0, '06:00', '06:00')
            self.assertEqual(result, {'counts': {'D17': 1}, 'total': 1, 'vehicles': 1})
            self.assertEqual(app.detector_counts(db, 0, '00:00', '01:00')['total'], 0)
            for dataset, start, end in [(0,'09:00','07:00'), (0,'24:00','25:00'), (0,'bad','12:00'), (99,'00:00','23:59')]:
                with self.assertRaises(ValueError):
                    app.detector_counts(db, dataset, start, end)

    def test_reimport_is_idempotent(self):
        """Повторно импортирует таблицы и проверяет отсутствие удвоения записей."""
        app.initialize_database(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM passages').fetchone()[0], 6715)

    def test_route_validation(self):
        """Проверяет нормализацию координат и отклонение неверных точек маршрута."""
        self.assertEqual(app.route_points('32,54;33,55'), '32.0,54.0;33.0,55.0')
        for value in ['', 'nan,54;32,55', '181,54;32,55', '32,91;32,55', 'https://example.com', '32,54,7;32,55']:
            with self.assertRaises(ValueError):
                app.route_points(value)

    def test_http(self):
        """Запускает временный HTTP-сервер и проверяет файлы, API и коды ошибок.

        Проверяет закрытость базы для HTTP и ответ 502 при сбое OSRM
        с подменённым сетевым вызовом. В конце останавливает сервер и поток.
        """
        server = app.make_server('127.0.0.1', 0, self.path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base + '/') as response:
                self.assertIn('text/html', response.headers['Content-Type'])
                self.assertIn(b'/client.py', response.read())
            with urlopen(base + '/client.py') as response:
                self.assertIn(b'class MapApplication:', response.read())
            with urlopen(base + '/api/bootstrap') as response:
                self.assertEqual(len(json.load(response)['datasets']), 2)
            with urlopen(base + '/api/detectors?dataset=1') as response:
                self.assertEqual(json.load(response)['total'], 3713)
            fake = {'code': 'Ok', 'routes': [{'distance': 1000, 'duration': 120,
                    'geometry': {'type': 'LineString', 'coordinates': [[32,54],[32.01,54]]}}]}
            with patch('app.fetch_route', return_value=fake):
                with urlopen(base + '/api/route?points=32,54;32.01,54&dataset=1&from=07:00&to=08:00') as response:
                    result = json.load(response)
                    self.assertEqual(result['trafficContext']['dataset'], 1)
                    self.assertEqual(result['trafficContext']['from'], '07:00')
                    self.assertIn('coverage', result['routes'][0]['traffic'])
            for path, code in [('/data/traffic.sqlite3',404),('/api/detectors?dataset=99',400),('/api/route?points=bad',400),('/api/route?points=32,54;33,55&dataset=99',400),('/api/route?points=32,54;33,55&from=09:00&to=07:00',400)]:
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

    def test_route_tls_fallback(self):
        """Проверяет резервный HTTPS-запрос через curl при ошибке TLS Python.

        Проверяет сохранение проверки сертификатов и передачу URL одним
        аргументом. При обычном сетевом сбое curl запускаться не должен.
        """
        response = subprocess.CompletedProcess([], 0, stdout=b'{"code":"Ok","routes":[]}')
        with patch('app.urllib.request.urlopen', side_effect=URLError(ssl.SSLError('handshake'))):
            with patch('app.subprocess.run', return_value=response) as run:
                self.assertEqual(app.fetch_route('32,54;33,55')['code'], 'Ok')
                command = run.call_args.args[0]
                self.assertNotIn('--insecure', command)
                self.assertNotIn('-k', command)
                self.assertTrue(command[-1].startswith('https://router.project-osrm.org/'))
                self.assertTrue(run.call_args.kwargs['check'])
        with patch('app.urllib.request.urlopen', side_effect=URLError('offline')):
            with patch('app.subprocess.run') as run:
                with self.assertRaises(URLError):
                    app.fetch_route('32,54;33,55')
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
