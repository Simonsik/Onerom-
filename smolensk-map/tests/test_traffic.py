"""Проверки выбора маршрута и границ применимости камерных наблюдений."""
import copy
import sqlite3
import unittest
from unittest.mock import patch
import app
from traffic import assess_route, camera_statistics, rank_routes, sensible_detour


def route(points, duration=100, distance=1200):
    """Создаёт маршрут с GeoJSON-линией для независимых синтетических сценариев."""
    return {'geometry': {'type': 'LineString', 'coordinates': points},
            'duration': duration, 'distance': distance}


def camera(name, lng, speed=10, samples=10, lat=54):
    """Создаёт камеру с известной базовой скоростью 50 км/ч."""
    return {'name': name, 'coordinates': [lat, lng], 'speed': speed,
            'referenceSpeed': 50, 'samples': samples}


class TrafficTests(unittest.TestCase):
    """Проверяет задержки, покрытие, цвета и выбор быстрого варианта."""
    def test_passage_threshold_applies_to_every_camera_at_normal_speed(self):
        for name in ('D1', 'D2', 'D18'):
            for count, level in ((19, 'free'), (20, 'jam'), (37, 'jam')):
                sensor = dict(camera(name, 32.01, speed=50), passages=count)
                traffic = assess_route(route([[32, 54], [32.02, 54]]), [sensor])['traffic']
                section = next(s for s in traffic['sections'] if s['level'] != 'unknown')
                self.assertEqual(section['level'], level)

    def test_shortest_route_is_preserved_when_service_returns_fastest_first(self):
        short = route([[32, 54], [32.02, 54]], 100, 1200)
        fast = route([[32, 54], [32, 54.01], [32.02, 54.01], [32.02, 54]], 200, 3000)
        cameras = [camera('A', 32.002), camera('B', 32.018)]
        with patch('app.fetch_route', return_value={'code': 'Ok', 'routes': [fast, short]}), \
                patch('app.detour_points', return_value=[]):
            result = app.find_routes('32,54;32.02,54', cameras, 0, '07:00', '08:00')
        self.assertEqual(result['routes'][0]['routeRole'], 'detour')
        self.assertEqual(result['routes'][0]['distance'], 3000)
        self.assertEqual(result['routes'][1]['distance'], 1200)
        self.assertIn('jam', [s['level'] for s in result['routes'][1]['traffic']['sections']])

    def test_slower_short_route_loses(self):
        """Короткий путь с замедлением уступает длинному по расчётному времени."""
        short = route([[32, 54], [32.01, 54], [32.02, 54]])
        detour = route([[32, 54], [32, 54.01], [32.02, 54.01], [32.02, 54]], 200, 3000)
        cameras = [camera('A', 32.002), camera('B', 32.018)]
        result = rank_routes({'code': 'Ok', 'routes': [short, detour]}, cameras, 0, '07:00', '08:00')
        self.assertEqual(result['routes'][0]['distance'], 3000)
        congested = result['routes'][1]['traffic']
        self.assertGreater(congested['duration'], 200)
        self.assertAlmostEqual(congested['coverage'], 80, places=1)
        self.assertIn('jam', [s['level'] for s in congested['sections']])

    def test_missing_sparse_and_single_camera_are_unknown(self):
        """Нет камер или недостаточно наблюдений — нет вывода о свободной дороге."""
        line = route([[32, 54], [32.02, 54]])
        for cameras in ([], [camera('A', 32.005, samples=2)],
                        [camera('A', 32), camera('B', 32.02, samples=2)],
                        [camera('A', 32, speed=None, samples=0), camera('B', 32.02)]):
            result = assess_route(line, cameras)['traffic']
            self.assertEqual(result['duration'], 100)
            self.assertEqual(result['coverage'], 0)
            self.assertTrue(all(s['level'] == 'unknown' for s in result['sections']))

    def test_single_slow_camera_marks_local_jam_and_triggers_detour_search(self):
        direct = route([[32, 54], [32.02, 54]])
        cameras = [camera('D2', 32.01, speed=8)]
        traffic = assess_route(direct, cameras)['traffic']
        jam = next(s for s in traffic['sections'] if s['level'] == 'jam')
        self.assertEqual(jam['source'], 'single-camera')
        self.assertEqual(jam['distance'], 300)
        self.assertGreater(traffic['delay'], 0)
        self.assertLess(traffic['coverage'], 100)
        with patch('app.fetch_route', return_value={'code': 'Ok', 'routes': [direct]}), \
                patch('app.detour_points', return_value=[]) as search:
            app.find_routes('32,54;32.02,54', cameras, 0, '00:00', '23:59')
        search.assert_called_once()

    def test_slower_alternative_remains_visible_for_congested_route(self):
        direct = route([[32, 54], [32.02, 54]])
        alternative = route([[32, 54], [32, 54.01], [32.02, 54.01], [32.02, 54]], 2000, 3000)
        with patch('app.fetch_route', return_value={'code': 'Ok', 'routes': [direct, alternative]}), \
                patch('app.detour_points', return_value=[]):
            result = app.find_routes('32,54;32.02,54', [camera('D2', 32.01, speed=8)], 0, '00:00', '23:59')
        self.assertEqual([r['routeRole'] for r in result['routes']], ['original', 'alternative'])
        self.assertGreater(result['routes'][1]['traffic']['duration'], result['routes'][0]['traffic']['duration'])

    def test_distant_camera_and_long_gap(self):
        """Далёкие камеры и промежутки больше 3 км не дают оценку загруженности."""
        result = assess_route(route([[32, 54], [32.1, 54]]),
                              [camera('A', 32), camera('B', 32.1), camera('C', 32.05, lat=55)])['traffic']
        self.assertEqual(len(result['cameras']), 2)
        self.assertEqual(result['coverage'], 0)

    def test_stopped_and_free_traffic(self):
        """Нулевые скорости дают конечную задержку, обычные — зелёный участок."""
        line = route([[32, 54], [32.02, 54]])
        for speed, level in ((0, 'jam'), (30, 'busy'), (50, 'free')):
            result = assess_route(line, [camera('A', 32, speed), camera('B', 32.02, speed)])['traffic']
            self.assertEqual(result['sections'][0]['level'], level)
            self.assertLessEqual(result['duration'], 1000)
            self.assertEqual(result['coverage'], 100)

    def test_statistics_use_interval_and_include_zero(self):
        """Медиана относится к фильтру, базовая скорость — к выбранной выборке."""
        with sqlite3.connect(':memory:') as db:
            db.executescript('CREATE TABLE sensors(name,lat,lng); CREATE TABLE passages(dataset,detector,time,plate,speed);')
            db.execute("INSERT INTO sensors VALUES ('A',54,32)")
            rows = [(0, 'A', '2025-10-15 07:00:00', 'x', speed) for speed in (0, 10, 20)]
            rows += [(0, 'A', '2025-10-15 08:00:00', 'y', 50), (1, 'A', '2025-10-15 07:00:00', 'z', 100)]
            db.executemany('INSERT INTO passages VALUES (?,?,?,?,?)', rows)
            stats = camera_statistics(db, 0, '07:00', '07:00')[0]
            self.assertEqual(stats['samples'], 3)
            self.assertEqual(stats['speed'], 10)
            self.assertEqual(stats['referenceSpeed'], 50)

    def test_equal_times_and_no_mutation(self):
        """При равном времени выбирается короткий путь; исходный ответ не меняется."""
        original = {'code': 'Ok', 'routes': [route([[32, 54], [32.02, 54]], distance=2000), route([[32, 54], [32.01, 54]], distance=1000)]}
        before = copy.deepcopy(original)
        result = rank_routes(original, [], 0, '00:00', '23:59')
        self.assertEqual(result['routes'][0]['distance'], 1000)
        self.assertEqual(original, before)

    def test_heavy_slow_flow_does_not_lower_reference(self):
        """Множество медленных машин не превращает затор в свободное движение."""
        with sqlite3.connect(':memory:') as db:
            db.executescript('CREATE TABLE sensors(name,lat,lng); CREATE TABLE passages(dataset,detector,time,plate,speed);')
            db.execute("INSERT INTO sensors VALUES ('A',54,32)")
            records = [(0, 'A', '2025-10-15 07:00:00', str(i), 8) for i in range(1200)]
            records += [(0, 'A', '2025-10-15 12:00:00', 'fast' + str(i), 50) for i in range(10)]
            db.executemany('INSERT INTO passages VALUES (?,?,?,?,?)', records)
            stats = camera_statistics(db, 0, '07:00', '09:00')[0]
            self.assertEqual(stats['samples'], 1200)
            self.assertEqual(stats['speed'], 8)
            self.assertEqual(stats['referenceSpeed'], 50)

    def test_search_finds_distinct_detour_and_keeps_original(self):
        """При единственном загруженном пути ищет объезд и сохраняет оба варианта."""
        direct = route([[32,54],[32.01,54],[32.02,54]])
        detour = route([[32,54],[32,54.01],[32.02,54.01],[32.02,54]], 200, 3000)
        initial = {'code': 'Ok', 'routes': [direct]}
        extra = {'code': 'Ok', 'routes': [detour], 'waypoints': [{'distance': 0}] * 3}
        cameras = [camera('A',32.002),camera('B',32.018)]
        with patch('app.fetch_route', side_effect=lambda coords, via=None: copy.deepcopy(initial if via is None else extra)):
            with patch('app.time.sleep'):
                result = app.find_routes('32,54;32.02,54', cameras, 0, '07:00', '08:00')
        self.assertEqual(len(result['routes']), 2)
        self.assertEqual(result['routes'][0]['distance'], 3000)
        self.assertIn('via', result['routes'][0])
        self.assertEqual(result['trafficContext']['detourAttempts'], 4)

    def test_detour_failure_keeps_congested_route(self):
        """Ошибка дополнительных запросов оставляет исходный путь доступным."""
        direct = route([[32,54],[32.02,54]])
        def fetch(coords, via=None):
            """Возвращает исходный путь и имитирует недоступность запроса объезда."""
            if via is not None:
                raise OSError('offline')
            return {'code': 'Ok', 'routes': [direct]}
        with patch('app.fetch_route', side_effect=fetch), patch('app.time.sleep'):
            result = app.find_routes('32,54;32.02,54', [camera('A',32),camera('B',32.02)], 0, '07:00', '08:00')
        self.assertEqual(len(result['routes']), 1)
        self.assertEqual(result['trafficContext']['detourFailures'], 4)

    def test_rejects_dead_end_and_wrong_destination(self):
        """Отклоняет заезд с возвратом и путь до другой конечной точки."""
        original = route([[32,54],[32.02,54]])
        dead_end = route([[32,54],[32.01,54],[32.01,54.01],[32.01,54],[32.02,54]], 200, 3000)
        wrong_end = route([[32,54],[32.02,54.01]])
        valid = route([[32,54],[32,54.01],[32.02,54.01],[32.02,54]], 200, 3000)
        self.assertFalse(sensible_detour(dead_end, original))
        self.assertFalse(sensible_detour(wrong_end, original))
        self.assertTrue(sensible_detour(valid, original))

    def test_only_best_beneficial_alternative_is_returned(self):
        """Возвращает исходный путь и только самый быстрый полезный объезд."""
        direct = route([[32,54],[32.02,54]])
        good = route([[32,54],[32,54.01],[32.02,54.01],[32.02,54]], 200, 3000)
        slower = route([[32,54],[32,53.99],[32.02,53.99],[32.02,54]], 300, 3500)
        cameras = [camera('A',32.002),camera('B',32.018)]
        with patch('app.fetch_route', return_value={'code': 'Ok', 'routes': [direct, slower, good]}):
            result = app.find_routes('32,54;32.02,54', cameras, 0, '07:00', '08:00')
        self.assertEqual(len(result['routes']), 2)
        self.assertEqual(result['routes'][0]['distance'], 3000)
        self.assertEqual(result['routes'][0]['routeRole'], 'detour')
        self.assertEqual(result['routes'][1]['routeRole'], 'original')

