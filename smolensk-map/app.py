"""Python 3.9+ application. Start: python3 app.py"""
import argparse
import json
import math
import mimetypes
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from data.import_xlsx import load_workbooks

ROOT = Path(__file__).resolve().parent
STATIC = {'/': 'index.html', '/index.html': 'index.html', '/style.css': 'style.css', '/script.js': 'script.js'}


def initialize_database(path, source=ROOT / 'data'):
    """Validate all workbooks before atomically replacing the imported snapshot."""
    payload = load_workbooks(source)
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS sensors (name TEXT PRIMARY KEY, lat REAL, lng REAL);
            CREATE TABLE IF NOT EXISTS datasets (id INTEGER PRIMARY KEY, name TEXT, raw_count INTEGER, issues TEXT, duplicates INTEGER);
            CREATE TABLE IF NOT EXISTS passages (dataset INTEGER, detector TEXT, time TEXT, plate TEXT, speed REAL);
            CREATE INDEX IF NOT EXISTS passages_time ON passages(dataset, time);
        ''')
        db.execute('DELETE FROM passages')
        db.execute('DELETE FROM datasets')
        db.execute('DELETE FROM sensors')
        db.executemany('INSERT INTO sensors VALUES (?, ?, ?)', [(s['name'], *s['coordinates']) for s in payload['sensors']])
        for i, dataset in enumerate(payload['datasets']):
            db.execute('INSERT INTO datasets VALUES (?, ?, ?, ?, ?)', (i, dataset['name'], dataset['rawCount'], json.dumps(dataset['issues'], ensure_ascii=False), dataset['duplicates']))
            db.executemany('INSERT INTO passages VALUES (?, ?, ?, ?, ?)', [(i, *r) for r in dataset['records']])


def bootstrap(db):
    sensors = [{'name': r[0], 'coordinates': list(r[1:])} for r in db.execute('SELECT name,lat,lng FROM sensors ORDER BY CAST(SUBSTR(name,2) AS INTEGER)')]
    datasets = [{'id': r[0], 'name': r[1], 'rawCount': r[2], 'issues': json.loads(r[3]), 'duplicates': r[4]} for r in db.execute('SELECT * FROM datasets ORDER BY id')]
    return {'sensors': sensors, 'datasets': datasets}


def detector_counts(db, dataset, start, end):
    try:
        if len(start) != 5 or len(end) != 5:
            raise ValueError()
        datetime.strptime(start, '%H:%M')
        datetime.strptime(end, '%H:%M')
    except ValueError:
        raise ValueError('Время должно быть в формате ЧЧ:ММ')
    if start > end:
        raise ValueError('Начало интервала должно быть не позже конца')
    if not db.execute('SELECT 1 FROM datasets WHERE id=?', (dataset,)).fetchone():
        raise ValueError('Неизвестная выборка')
    args = (dataset, start, end)
    clause = 'dataset=? AND substr(time,12,5) BETWEEN ? AND ?'
    counts = dict(db.execute(f'SELECT detector,COUNT(*) FROM passages WHERE {clause} GROUP BY detector', args))
    total, vehicles = db.execute(f'SELECT COUNT(*),COUNT(DISTINCT plate) FROM passages WHERE {clause}', args).fetchone()
    return {'counts': counts, 'total': total, 'vehicles': vehicles}


def route_points(value):
    try:
        parts = value.split(';')
        if len(parts) != 2:
            raise ValueError()
        coords = [list(map(float, part.split(','))) for part in parts]
        if any(len(c) != 2 or not all(math.isfinite(v) for v in c) or abs(c[0]) > 180 or abs(c[1]) > 90 for c in coords):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Нужны две точки в формате долгота,широта;долгота,широта')
    return ';'.join(','.join(str(v) for v in c) for c in coords)


class Handler(BaseHTTPRequestHandler):
    def json_response(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == '/api/bootstrap':
                with sqlite3.connect(self.server.database) as db:
                    self.json_response(bootstrap(db))
            elif parsed.path == '/api/detectors':
                with sqlite3.connect(self.server.database) as db:
                    result = detector_counts(db, int(query.get('dataset', ['0'])[0]), query.get('from', ['00:00'])[0], query.get('to', ['23:59'])[0])
                self.json_response(result)
            elif parsed.path == '/api/route':
                coords = route_points(query.get('points', [''])[0])
                url = f'https://router.project-osrm.org/route/v1/driving/{coords}?alternatives=true&overview=full&geometries=geojson&steps=false'
                try:
                    req = urllib.request.Request(url, headers={'User-Agent': 'SmolenskMap/1.0'})
                    with urllib.request.urlopen(req, timeout=15) as response:
                        result = json.load(response)
                    self.json_response(result)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                    self.json_response({'error': 'Сервис маршрутов недоступен. Попробуйте позже.'}, 502)
            elif parsed.path in STATIC:
                path = ROOT / STATIC[parsed.path]
                body = path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', (mimetypes.guess_type(path)[0] or 'application/octet-stream') + '; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.json_response({'error': 'Не найдено'}, 404)
        except ValueError as error:
            self.json_response({'error': str(error)}, 400)


def make_server(host, port, database):
    server = ThreadingHTTPServer((host, port), Handler)
    server.database = str(database)
    return server


def main():
    parser = argparse.ArgumentParser(description='Карта транспортных потоков Смоленска')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    database = ROOT / 'data' / 'traffic.sqlite3'
    initialize_database(database)
    server = make_server(args.host, args.port, database)
    print(f'Карта готова: http://{args.host}:{args.port} (Ctrl+C для остановки)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
