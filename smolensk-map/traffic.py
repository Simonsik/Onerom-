"""Оценка исторической загруженности маршрутов по скоростям детекторов."""
import math
from collections import defaultdict
from statistics import median

MATCH_METERS = 35
MIN_SAMPLES = 3
MAX_SECTION_METERS = 3000
SINGLE_CAMERA_RADIUS = 150
JAM_PASSAGES = 20


def delay_factor(camera):
    """Общий порог потока для всех камер; задержка по потоку условная."""
    speed_factor = camera['referenceSpeed'] / max(camera['speed'], 1)
    flow_factor = 3 if camera.get('passages', 0) >= JAM_PASSAGES else 1
    return min(10, max(1, speed_factor, flow_factor))


def camera_statistics(db, dataset, start, end):
    """Возвращает скорости камер за интервал и базовую скорость по выборке.

    База — 85-й процентиль двадцати самых быстрых положительных измерений.
    Большой поток медленных машин не должен понижать саму базовую скорость.
    Медиана интервала учитывает и остановки (нулевую скорость).
    Меньше трёх измерений считаются недостаточными для оценки участка.
    """
    all_speeds, selected = defaultdict(list), defaultdict(list)
    for name, timestamp, speed in db.execute(
            'SELECT detector,time,speed FROM passages WHERE dataset=?', (dataset,)):
        if speed > 0:
            all_speeds[name].append(speed)
        if start <= timestamp[11:16] <= end:
            selected[name].append(speed)
    stats = []
    for name, lat, lng in db.execute('SELECT name,lat,lng FROM sensors'):
        reference = sorted(all_speeds[name])[-20:]
        values = selected[name]
        stats.append({
            'name': name, 'coordinates': [lat, lng], 'samples': len(values),
            'passages': len(values),
            'speed': median(values) if values else None,
            'referenceSpeed': reference[math.ceil(.85 * len(reference)) - 1] if reference else None,
        })
    return stats


def project(point, origin):
    """Переводит [долгота, широта] в локальные метры относительно origin.

    Приближение предназначено для городских маршрутов, не для всей планеты.
    """
    return ((point[0] - origin[0]) * 111320 * math.cos(math.radians(origin[1])),
            (point[1] - origin[1]) * 111320)


def route_geometry(coordinates):
    """Возвращает плоские координаты и накопленные длины сегментов в метрах."""
    xy = [project(point, coordinates[0]) for point in coordinates]
    lengths = [0.0]
    for a, b in zip(xy, xy[1:]):
        lengths.append(lengths[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return xy, lengths


def locate_camera(camera, coordinates, xy, lengths):
    """Находит ближайшую проекцию камеры на линию маршрута.

    Возвращает расстояние до линии и положение вдоль неё. Привязка
    геометрическая: направление движения и параллельные дороги неизвестны.
    """
    lat, lng = camera['coordinates']
    px, py = project([lng, lat], coordinates[0])
    best = (float('inf'), 0.0)
    for i, (a, b) in enumerate(zip(xy, xy[1:])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        square = dx * dx + dy * dy
        t = max(0, min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / square)) if square else 0
        distance = math.hypot(px - a[0] - t * dx, py - a[1] - t * dy)
        if distance < best[0]:
            best = distance, lengths[i] + t * (lengths[i + 1] - lengths[i])
    return best


def slice_geometry(coordinates, lengths, start, end):
    """Вырезает линию между двумя положениями вдоль маршрута.

    Интерполирует граничные точки и сохраняет все изгибы внутри участка.
    """
    def at(position):
        """Возвращает координату на заданном расстоянии вдоль линии."""
        for i in range(len(lengths) - 1):
            if lengths[i + 1] >= position:
                span = lengths[i + 1] - lengths[i]
                t = (position - lengths[i]) / span if span else 0
                return [coordinates[i][axis] + t * (coordinates[i + 1][axis] - coordinates[i][axis]) for axis in (0, 1)]
        return coordinates[-1]
    return [at(start)] + [point for point, offset in zip(coordinates, lengths) if start < offset < end] + [at(end)]


def assess_route(route, cameras):
    """Оценивает задержки между последовательными камерами на маршруте.

    Участок оценивается при наличии трёх наблюдений на каждой границе
    и длине до 3 км. Коэффициент задержки — среднее отношений базовой
    скорости к медианной, ограниченное диапазоном 1–10. Базовое время
    OSRM распределяется по длине; это модель, а не измеренное время пути.
    При единственной камере с достаточными данными оценивается только
    локальная зона ±150 м; это предположение, не измеренная длина затора.
    Неизвестные участки сохраняют базовое время и серый цвет.
    """
    coordinates = route['geometry']['coordinates']
    if len(coordinates) < 2:
        raise ValueError('Маршрут не содержит линии дороги')
    xy, lengths = route_geometry(coordinates)
    total = lengths[-1]
    matched = []
    for camera in cameras:
        distance, offset = locate_camera(camera, coordinates, xy, lengths)
        if distance <= MATCH_METERS:
            matched.append(dict(camera, offset=offset, distanceToRoute=round(distance, 1)))
    matched.sort(key=lambda camera: camera['offset'])
    sections, covered, delay = [], 0.0, 0.0
    cursor = 0.0
    # Одна камера измеряет скорость локально: не распространяем её на весь путь.
    if len(matched) == 1:
        camera = matched[0]
        if camera['samples'] >= MIN_SAMPLES and camera['referenceSpeed'] and camera['speed'] is not None:
            start = max(0, camera['offset'] - SINGLE_CAMERA_RADIUS)
            end = min(total, camera['offset'] + SINGLE_CAMERA_RADIUS)
            if start > 0:
                sections.append({'level': 'unknown', 'geometry': slice_geometry(coordinates, lengths, 0, start)})
            factor = delay_factor(camera)
            sections.append({
                'from': camera['name'], 'to': camera['name'], 'source': 'single-camera',
                'level': 'jam' if 1 / factor < .4 else 'busy' if 1 / factor < .7 else 'free',
                'distance': round(end - start), 'speed': camera['speed'], 'factor': round(factor, 2),
                'geometry': slice_geometry(coordinates, lengths, start, end),
            })
            covered = end - start
            delay = route['duration'] * covered / total * (factor - 1) if total else 0
            cursor = end
    for left, right in zip(matched, matched[1:]):
        start, end = left['offset'], right['offset']
        if end - start < 1:
            continue
        if start > cursor:
            sections.append({'level': 'unknown', 'geometry': slice_geometry(coordinates, lengths, cursor, start)})
        known = (end - start <= MAX_SECTION_METERS and all(
            c['samples'] >= MIN_SAMPLES and c['referenceSpeed'] for c in (left, right)))
        section = {'from': left['name'], 'to': right['name'], 'level': 'unknown',
                   'distance': round(end - start), 'geometry': slice_geometry(coordinates, lengths, start, end)}
        if known:
            factor = min(10, max(1, sum(delay_factor(c) for c in (left, right)) / 2))
            if any(c.get('passages', 0) >= JAM_PASSAGES for c in (left, right)):
                factor = max(3, factor)
            ratio = 1 / factor
            section.update(level='jam' if ratio < .4 else 'busy' if ratio < .7 else 'free',
                           speed=round((left['speed'] + right['speed']) / 2, 1),
                           factor=round(factor, 2))
            covered += end - start
            delay += route['duration'] * (end - start) / total * (factor - 1) if total else 0
        sections.append(section)
        cursor = end
    if cursor < total:
        sections.append({'level': 'unknown', 'geometry': slice_geometry(coordinates, lengths, cursor, total)})
    return dict(route, traffic={
        'duration': round(route['duration'] + delay, 1), 'delay': round(delay, 1),
        'coverage': round(covered / total * 100, 1) if total else 0,
        'cameras': [{k: v for k, v in camera.items() if k != 'offset'} for camera in matched],
        'sections': sections,
    })




def rank_routes(result, cameras, dataset, start, end):
    """Оценивает все полученные варианты и сортирует по времени с задержками.

    При равном времени предпочитает меньшую длину. Поиск конечен и не
    гарантирует оптимум вне набора альтернатив, возвращённых OSRM.
    """
    if result.get('code') != 'Ok':
        return result
    routes = [assess_route(route, cameras) for route in result.get('routes', [])]
    routes.sort(key=lambda route: (route['traffic']['duration'], route['distance']))
    return dict(result, routes=routes, trafficContext={
        'dataset': dataset, 'from': start, 'to': end, 'historical': True,
        'matchingRadius': MATCH_METERS, 'minSamples': MIN_SAMPLES,
        'method': 'camera-speed-and-passage-threshold', 'jamPassages': JAM_PASSAGES, 'candidates': len(routes),
    })


def detour_points(route):
    """Возвращает четыре точки по сторонам от середины загруженного участка.

    Смещения 800 и 1600 м служат только точками поиска: сам путь всегда
    строится OSRM по автомобильным дорогам, не прямой линией.
    """
    section = next(s for s in route['traffic']['sections'] if s['level'] in ('busy', 'jam'))
    coordinates = section['geometry']
    _, lengths = route_geometry(coordinates)
    middle = slice_geometry(coordinates, lengths, lengths[-1] / 2, lengths[-1] / 2)[0]
    dx, dy = project(coordinates[-1], coordinates[0])
    length = math.hypot(dx, dy) or 1
    scale = 111320 * math.cos(math.radians(middle[1]))
    return [[middle[0] - dy / length * offset / scale,
             middle[1] + dx / length * offset / 111320]
            for offset in (800, -800, 1600, -1600)]


def distinct_route(candidate, routes):
    """Отклоняет геометрические дубликаты: нужен отход хотя бы на 100 м.

    Сравнивает до 31 равномерно выбранной вершины с каждым существующим
    маршрутом; это приближённая проверка различий, а не сравнение ID дорог.
    """
    points = candidate['geometry']['coordinates']
    sample = points[::max(1, math.ceil(len(points) / 30))] + [points[-1]]
    for existing in routes:
        coordinates = existing['geometry']['coordinates']
        xy, lengths = route_geometry(coordinates)
        if all(locate_camera({'coordinates': [p[1], p[0]]}, coordinates, xy, lengths)[0] <= 100 for p in sample):
            return False
    return True


def sensible_detour(candidate, original):
    """Проверяет, что объезд соединяет те же концы и не содержит тупиковых петель.

    Отклоняет возврат в уже пройденную вершину после 25 м пути и обратный
    проход вдоль того же отрезка длиннее 20 м (допуск 8 м для геометрии).
    Это геометрический фильтр, а не проверка всех дорожных ограничений.
    """
    points = candidate['geometry']['coordinates']
    base = original['geometry']['coordinates']
    if len(points) < 2:
        return False
    for point, endpoint in ((points[0], base[0]), (points[-1], base[-1])):
        if math.hypot(*project(point, endpoint)) > 60:
            return False
    if candidate['distance'] > max(original['distance'] * 3, original['distance'] + 4000):
        return False
    xy, lengths = route_geometry(points)
    visited = {}
    for point, distance in zip(points, lengths):
        key = tuple(round(value, 6) for value in point)
        if key in visited and distance - visited[key] > 25:
            return False
        visited.setdefault(key, distance)
    segments = [(a, b) for a, b in zip(xy, xy[1:]) if math.hypot(b[0] - a[0], b[1] - a[1]) > 1]
    for i, (a, b) in enumerate(segments):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        for c, d in segments[i + 1:]:
            ex, ey = d[0] - c[0], d[1] - c[1]
            if (dx * ex + dy * ey) / (length * math.hypot(ex, ey)) > -.95:
                continue
            if max(abs(dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / length for p in (c, d)) > 8:
                continue
            offsets = [((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length for p in (c, d)]
            if min(length, max(offsets)) - max(0, min(offsets)) > 20:
                return False
    return True
