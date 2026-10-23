"""Python 3.9+ application. Start: python3 app.py"""
import argparse
import json
import math
import mimetypes
import os
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from data.import_xlsx import load_workbooks
from traffic import camera_statistics, rank_routes, detour_points, distinct_route, sensible_detour

ROOT = Path(__file__).resolve().parent
STATIC = {'/': 'index.html', '/index.html': 'index.html', '/style.css': 'style.css', '/client.py': 'client.py', '/route_view.py': 'route_view.py'}


def initialize_database(path, source=ROOT / 'data'):
    """Создаёт схему SQLite и заменяет снимок данных из Excel.

    path — путь к базе, source — каталог таблиц. Сначала проверяет все
    таблицы; удаление старых записей и вставку новых выполняет одной транзакцией.
    """
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
    """Возвращает детекторы и описание выборок для запуска интерфейса.

    db — открытое соединение SQLite. Результат — словарь sensors/datasets;
    детекторы упорядочены по числу в имени, замечания импорта разобраны из JSON.
    """
    sensors = [{'name': r[0], 'coordinates': list(r[1:])} for r in db.execute('SELECT name,lat,lng FROM sensors ORDER BY CAST(SUBSTR(name,2) AS INTEGER)')]
    datasets = [{'id': r[0], 'name': r[1], 'rawCount': r[2], 'issues': json.loads(r[3]), 'duplicates': r[4]} for r in db.execute('SELECT * FROM datasets ORDER BY id')]
    return {'sensors': sensors, 'datasets': datasets}


def detector_counts(db, dataset, start, end):
    """Считает проезды по детекторам и число уникальных машин.

    dataset — номер выборки, start/end — время ЧЧ:ММ внутри одного дня.
    Обе границы включаются до минуты. Возвращает counts, total и vehicles;
    при неверном времени, порядке границ или выборке вызывает ValueError.
    """
    try:
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
    """Проверяет две точки маршрута и нормализует их для OSRM.

    Принимает строку долгота,широта;долгота,широта. Проверяет количество
    координат, конечность чисел и диапазоны. Возвращает строку чисел float;
    неверные данные отклоняет через ValueError.
    """
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


def fetch_route(coords, via=None):
    """Получает маршруты OSRM для проверенной строки координат coords.

    При несовместимости TLS системного Python повторяет HTTPS-запрос
    через системный curl с проверкой сертификата и ограничением времени.
    Координаты проверяет повторно; команды запускает без shell.
    via — необязательная промежуточная точка [долгота, широта] для объезда.
    Возвращает JSON-ответ сервиса; ошибки передаёт вызывающему коду.
    """
    coords = route_points(coords)
    alternatives = '3'
    if via is not None:
        middle = route_points(f'{via[0]},{via[1]};{via[0]},{via[1]}').split(';')[0]
        start, end = coords.split(';')
        coords = ';'.join((start, middle, end))
        alternatives = 'false'
    url = f'https://router.project-osrm.org/route/v1/driving/{coords}?alternatives={alternatives}&overview=full&geometries=geojson&steps=false'
    req = urllib.request.Request(url, headers={'User-Agent': 'SmolenskMap/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=6) as response:
            return json.load(response)
    except urllib.error.URLError as error:
        if not isinstance(error.reason, ssl.SSLError):
            raise
    except ssl.SSLError:
        pass
    response = subprocess.run(
        ['curl', '--fail', '--silent', '--show-error', '--connect-timeout', '5',
         '--max-time', '10', '--user-agent', 'SmolenskMap/1.0', url],
        capture_output=True, check=True, timeout=11,
    )
    return json.loads(response.stdout)


def find_routes(coords, cameras, dataset, start, end):
    """Сравнивает исходные пути и до четырёх объездов через промежуточные точки.

    Если лучший путь содержит замедление, проверяет точки по обе стороны
    от него. Не принимает дубликаты и точки дальше 300 м от дороги.
    Между дополнительными запросами выдерживает секунду; поиск ограничен
    45 секундами плюс время текущего запроса. Сбои объездов не скрывают
    исходный путь. Оптимум относится только к проверенным вариантам.
    """
    raw = fetch_route(coords)
    if raw.get('code') != 'Ok' or not raw.get('routes'):
        return raw
    shortest = min(raw['routes'], key=lambda r: (r['distance'], r['duration']))
    original = dict(shortest, routeRole='original')
    candidates = [original] + [r for r in raw['routes'] if r is not shortest and sensible_detour(r, original)]
    result = rank_routes(dict(raw, routes=candidates), cameras, dataset, start, end)
    routes = result.get('routes', [])
    if result.get('code') != 'Ok' or not routes:
        return result
    original_scored = next(r for r in routes if r.get('routeRole') == 'original')
    congested = any(s['level'] in ('busy', 'jam') for s in original_scored['traffic']['sections'])
    attempts, failures = 0, 0
    if congested:
        deadline = time.monotonic() + 45
        for via in detour_points(original_scored):
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
            attempts += 1
            try:
                extra = fetch_route(coords, via=via)
                waypoints = extra.get('waypoints', [])
                if extra.get('code') != 'Ok' or len(waypoints) != 3 or waypoints[1].get('distance', 0) > 300:
                    continue
                for candidate in extra.get('routes', []):
                    if sensible_detour(candidate, original) and distinct_route(candidate, routes):
                        candidate['via'] = via
                        routes.append(candidate)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, subprocess.SubprocessError):
                failures += 1
        result = rank_routes(dict(result, routes=routes), cameras, dataset, start, end)
    result['trafficContext'].update(detourAttempts=attempts, detourFailures=failures)
    original_scored = next(r for r in result['routes'] if r.get('routeRole') == 'original')
    alternatives = [r for r in result['routes'] if r.get('routeRole') != 'original'
                    and distinct_route(r, [original_scored])]
    better = [r for r in alternatives if r['traffic']['duration'] < original_scored['traffic']['duration']]
    if better:
        result['routes'] = [dict(better[0], routeRole='detour'), original_scored]
    elif congested and alternatives:
        result['routes'] = [original_scored, dict(alternatives[0], routeRole='alternative')]
    else:
        result['routes'] = [original_scored]
    result['trafficContext']['displayed'] = len(result['routes'])
    return result


class Handler(BaseHTTPRequestHandler):
    """Обрабатывает HTTP-запросы к API и разрешённым файлам интерфейса."""
    def json_response(self, value, status=200):
        """Отправляет value как JSON с HTTP-кодом status.

        Использует UTF-8, запрещает NaN и отключает кеширование ответа.
        Заголовок Content-Length вычисляет по закодированным байтам.
        """
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Обрабатывает GET: начальные данные, счётчики, маршрут или статику.

        Базу открывает на время запроса. Маршруты запрашивает у OSRM
        с ограничением времени и резервным curl при ошибке TLS. Ошибки параметров возвращает как 400,
        недоступность маршрутизации — 502, неизвестные пути — 404.
        """
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
                dataset = int(query.get('dataset', ['0'])[0])
                start, end = query.get('from', ['00:00'])[0], query.get('to', ['23:59'])[0]
                with sqlite3.connect(self.server.database) as db:
                    detector_counts(db, dataset, start, end)
                    cameras = camera_statistics(db, dataset, start, end)
                try:
                    result = find_routes(coords, cameras, dataset, start, end)
                    self.json_response(result)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, subprocess.SubprocessError) as error:
                    self.log_error('Ошибка маршрутизации: %s', error)
                    self.json_response({'error': 'Не удалось связаться с сервисом маршрутов'}, 502)
            elif parsed.path in STATIC:
                path = ROOT / STATIC[parsed.path]
                body = path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', (mimetypes.guess_type(path)[0] or 'application/octet-stream') + '; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)
            else:
                self.json_response({'error': 'Не найдено'}, 404)
        except ValueError as error:
            self.json_response({'error': str(error)}, 400)


def make_server(host, port, database):
    """Создаёт многопоточный HTTP-сервер на host:port.

    Сохраняет путь database для обработчиков и возвращает сервер
    без запуска цикла обслуживания; port=0 выбирает свободный порт.
    """
    server = ThreadingHTTPServer((host, port), Handler)
    server.database = str(database)
    return server


def source_snapshot():
    """Возвращает версии серверных модулей и исходных таблиц для автоперезапуска.

    Файлы интерфейса читаются при каждом запросе и перезапуска не требуют.
    Базу SQLite и кеш Python не отслеживает, чтобы избежать цикла рестартов.
    """
    paths = [ROOT / 'app.py', ROOT / 'traffic.py', *sorted((ROOT / 'data').glob('*.py')),
             *sorted((ROOT / 'data').glob('*.xlsx'))]
    return {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in paths}


def serve_with_reload(server):
    """Обслуживает запросы и проверяет изменения исходников каждые полсекунды.

    При изменениях проверяет синтаксис серверных модулей, закрывает порт
    и заменяет процесс свежим Python с прежними аргументами запуска.
    При синтаксической ошибке продолжает работать до следующего сохранения.
    """
    snapshot = source_snapshot()
    server.timeout = 0.5
    while True:
        server.handle_request()
        updated = source_snapshot()
        if updated == snapshot:
            continue
        snapshot = updated
        try:
            for path in updated:
                if path.suffix == '.py':
                    compile(path.read_bytes(), str(path), 'exec')
        except (SyntaxError, OSError) as error:
            print(f'Не удалось обновить сервер: {error}', flush=True)
            continue
        print('Файлы изменились — перезапускаем сервер…', flush=True)
        server.server_close()
        os.execv(sys.executable, [sys.executable, str(ROOT / 'app.py'), *sys.argv[1:]])


def main():
    """Запускает приложение с адресом и портом из командной строки.

    Обновляет SQLite из таблиц, обслуживает запросы до Ctrl+C
    и закрывает сервер при завершении. По умолчанию автоматически
    перезапускается при изменении серверного кода или таблиц.
    """
    parser = argparse.ArgumentParser(description='Карта транспортных потоков Смоленска')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-reload', action='store_true', help='Отключить автоматический перезапуск')
    args = parser.parse_args()
    database = ROOT / 'data' / 'traffic.sqlite3'
    initialize_database(database)
    server = make_server(args.host, args.port, database)
    print(f'Карта готова: http://{args.host}:{args.port} (Ctrl+C для остановки)', flush=True)
    try:
        if args.no_reload:
            server.serve_forever()
        else:
            serve_with_reload(server)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
