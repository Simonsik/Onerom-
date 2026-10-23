"""Browser interface, executed by Brython. Leaflet supplies the map renderer."""
import json
import math
from browser import document, window, ajax, html, timer


def js(value):
    """Преобразует вложенные Python-данные в объекты JavaScript через JSON.

    Нужно для передачи списков, словарей и настроек в Leaflet.
    Возвращает объект JavaScript; value должен быть сериализуемым в JSON.
    """
    return window.JSON.parse(json.dumps(value))


def status(text):
    """Показывает text в строке состояния карты как безопасный текст."""
    document['status'].text = text


def get_json(url, success, failure):
    """Асинхронно запрашивает JSON; для поиска объезда ждёт до 90 секунд.

    При HTTP 200 вызывает success со словарём или другим JSON-значением;
    при ошибке ответа, разбора или тайм-ауте вызывает failure с сообщением.
    Возвращает AJAX-запрос, который можно отменить через abort().
    """
    def complete(req):
        """Разбирает ответ req и направляет результат в success или failure.

        Ошибки самого обработчика success не перехватывает.
        """
        try:
            value = json.loads(req.text)
            if req.status != 200:
                raise ValueError(value.get('error', 'Ошибка сервера'))
        except Exception as error:
            failure(str(error))
            return
        success(value)
    return ajax.get(url, cache=True, timeout=90 if url.startswith('/api/route?') else 20, oncomplete=complete,
                    ontimeout=lambda: failure('Сервис не ответил вовремя'))


COLORS = {'free': '#16a36a', 'busy': '#e9a323', 'jam': '#e04444'}
LEVELS = {'free': 'Свободно', 'busy': 'Плотное движение', 'jam': 'Пробка'}


def valid_coord(value):
    """Проверяет список [широта, долгота] и возвращает bool.

    Допускает только конечные int/float в диапазонах ±90 и ±180;
    логические значения координатами не считает.
    """
    return (isinstance(value, list) and len(value) == 2
            and all(type(v) in (int, float) and math.isfinite(v) for v in value)
            and abs(value[0]) <= 90 and abs(value[1]) <= 180)


def validate(value):
    """Проверяет структуру импортируемого JSON дорог и камер.

    Проверяет demo, названия, уровни движения, координаты и ограничения
    числа объектов/точек. Возвращает исходный словарь без изменений;
    при нарушениях вызывает исключение, обрабатываемое импортом.
    """
    if not isinstance(value, dict) or type(value.get('demo')) is not bool or not all(isinstance(value.get(k), list) for k in ('roads', 'cameras')):
        raise ValueError('Нужны demo (true/false), roads и cameras')
    if len(value['roads']) > 500 or len(value['cameras']) > 2000:
        raise ValueError('Слишком много объектов')
    for road in value['roads']:
        if (not isinstance(road, dict) or not isinstance(road.get('name'), str)
                or road.get('level') not in COLORS or not isinstance(road.get('coordinates'), list)
                or not 2 <= len(road['coordinates']) <= 10000
                or not all(valid_coord(c) for c in road['coordinates'])):
            raise ValueError('Проверьте название, level и координаты дорог')
    for camera in value['cameras']:
        if not isinstance(camera, dict) or not isinstance(camera.get('name'), str) or not valid_coord(camera.get('coordinates')):
            raise ValueError('Проверьте названия и координаты камер')
    return value


def popup(title, detail):
    """Создаёт DOM-блок всплывающей подсказки из title и detail.

    Вставляет значения как текст, поэтому HTML из названий не выполняется.
    """
    node = html.DIV()
    node <= html.STRONG(title)
    node <= html.P(detail)
    return node


class MapApplication:
    """Хранит состояние карты и связывает Leaflet, интерфейс и Python API."""
    def __init__(self, bootstrap):
        """Создаёт карту, слои, состояние точек и обработчики событий.

        bootstrap содержит sensors/datasets от сервера. Запускает загрузку
        счётчиков, рисует исходные данные и включает выбор точки А.
        """
        self.bootstrap = bootstrap
        self.data = {'demo': False, 'roads': [], 'cameras': bootstrap['sensors']}
        self.imported = False
        self.points = {'a': None, 'b': None}
        self.markers = {'a': None, 'b': None}
        self.selected = 'a'
        self.routes = []
        self.active_route = 0
        self.sequence = 0
        self.detector_sequence = 0
        self.request = None
        self.pending = None
        self.warned = False
        self.L = window.L
        self.map = self.L.map('map').setView(js([54.7826, 32.0453]), 13)
        tiles = self.L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', js({
            'maxZoom': 19, 'attribution': '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'})).addTo(self.map)
        self.map.createPane('traffic')
        self.map.getPane('traffic').style.zIndex = 450
        self.map.createPane('route-traffic')
        self.map.getPane('route-traffic').style.zIndex = 460
        self.traffic = self.L.layerGroup().addTo(self.map)
        self.cameras = self.L.layerGroup().addTo(self.map)
        self.route_layer = self.L.layerGroup().addTo(self.map)
        self.route_traffic = self.L.layerGroup().addTo(self.map)
        self.map.on('click', self.map_click)
        tiles.on('tileerror', self.tile_error)
        try:
            saved_theme = window.localStorage.getItem('smolensk-map-theme')
        except Exception:
            saved_theme = None
        self.set_map_theme(saved_theme or 'graphite')
        document['map-theme'].bind('change', lambda event: self.set_map_theme(event.target.value))
        for key in ('a', 'b'):
            document['select-' + key].bind('click', lambda event, key=key: self.select(key))
        document['swap'].bind('click', self.swap)
        document['reset'].bind('click', self.reset)
        document['center'].bind('click', lambda event: self.map.setView(js([54.7826, 32.0453]), 13))
        document['retry'].bind('click', lambda event: self.schedule())
        document['import-data'].bind('change', self.import_data)
        for name, layer in (('show-traffic', self.traffic), ('show-cameras', self.cameras)):
            document[name].bind('change', lambda event, layer=layer: self.map.addLayer(layer) if event.target.checked else self.map.removeLayer(layer))
        for name in ('dataset', 'time-from', 'time-to'):
            document[name].bind('change', self.filters_changed)
        window.getRoutePoints = lambda: js({'start': self.points['a'], 'end': self.points['b']})
        self.render_data()
        self.render_detectors()
        self.select('a')

    def set_map_theme(self, theme):
        """Меняет оттенок подложки, сохраняя цвета маршрутов и камер."""
        if theme not in ('graphite', 'midnight', 'plum', 'light'):
            theme = 'graphite'
        document.body.setAttribute('data-map-theme', theme)
        document['map-theme'].value = theme
        try:
            window.localStorage.setItem('smolensk-map-theme', theme)
        except Exception:
            pass

    def tile_error(self, event):
        """Один раз сообщает об ошибке загрузки тайлов; event — событие Leaflet."""
        if not self.warned:
            self.warned = True
            status('Часть карты не загрузилась. Проверьте подключение к интернету.')

    def update(self):
        """Обновляет координаты, активные кнопки, доступность действий и курсор.

        Считывает текущее состояние points/selected, не запрашивая сервер.
        """
        for key, point in self.points.items():
            button = document['select-' + key]
            button.classList.toggle('active', self.selected == key)
            button.setAttribute('aria-pressed', str(self.selected == key).lower())
            document['coordinates-' + key].text = f"{point['lat']:.5f}, {point['lng']:.5f}" if point else 'Выберите точку на карте'
        document['swap'].disabled = not all(self.points.values())
        document['reset'].disabled = not any(self.points.values())
        self.map.getContainer().style.cursor = 'crosshair' if self.selected else ''

    def select(self, key):
        """Включает выбор точки key (a или b) и показывает подсказку на карте."""
        self.selected = key
        self.update()
        status('Нажмите на карту: ' + ('откуда едем?' if key == 'a' else 'куда едем?'))

    def marker(self, key):
        """Создаёт перетаскиваемый маркер точки key или обновляет его положение.

        Для нового маркера назначает обработчик окончания перетаскивания.
        """
        if self.markers[key] is not None:
            self.markers[key].setLatLng(js(self.points[key]))
            return
        letter = 'А' if key == 'a' else 'Б'
        icon = self.L.divIcon(js({'className': '', 'html': f'<div class="map-marker {key}">{letter}</div>', 'iconSize': [36, 36], 'iconAnchor': [18, 18]}))
        self.markers[key] = self.L.marker(js(self.points[key]), js({'draggable': True, 'title': 'Точка ' + letter, 'alt': 'Точка ' + letter})).setIcon(icon).addTo(self.map)
        def dragged(event):
            """Сохраняет координаты перетащенного маркера и планирует новый маршрут."""
            point = self.markers[key].getLatLng()
            self.points[key] = {'lat': point.lat, 'lng': point.lng}
            self.update()
            self.schedule()
        self.markers[key].on('dragend', dragged)

    def map_click(self, event):
        """Сохраняет выбранную на карте точку из event.latlng.

        После первой точки предлагает вторую; когда обе заданы,
        выключает режим выбора и планирует расчёт маршрутов.
        """
        if not self.selected:
            return
        key = self.selected
        self.points[key] = {'lat': event.latlng.lat, 'lng': event.latlng.lng}
        self.marker(key)
        if not self.points['a']:
            self.select('a')
        elif not self.points['b']:
            self.select('b')
        else:
            self.selected = None
            self.update()
            self.schedule()

    def invalidate(self):
        """Отменяет отложенный расчёт и запрос, очищает маршруты и кнопку повтора.

        Увеличивает sequence, чтобы ответы прежних запросов не меняли интерфейс.
        """
        self.sequence += 1
        if self.pending is not None:
            timer.clear_timeout(self.pending)
            self.pending = None
        if self.request is not None:
            self.request.abort()
            self.request = None
        self.routes = []
        self.route_layer.clearLayers()
        self.route_traffic.clearLayers()
        document['routes'].clear()
        document['route-traffic'].text = ''
        document['retry'].hidden = True

    def schedule(self):
        """Сбрасывает старый маршрут и откладывает расчёт на 350 миллисекунд.

        Если одна из точек отсутствует, новый запрос не запускает.
        """
        self.invalidate()
        if all(self.points.values()):
            status('Сравниваем пути и ищем объезд затора… Это может занять до 90 секунд.')
            self.pending = timer.set_timeout(self.build, 350)

    def build(self):
        """Запрашивает маршруты между текущими А и Б через серверный API.

        Фиксирует номер sequence для защиты от устаревших ответов;
        передаёт координаты в порядке долгота, широта.
        """
        self.pending = None
        token = self.sequence
        def failed(message):
            """Показывает ошибку расчёта и кнопку повтора, если запрос ещё актуален."""
            if token == self.sequence:
                status(message.rstrip('. ') + '. Попробуйте изменить точки или повторить запрос.')
                document['retry'].hidden = False
        def received(result):
            """Проверяет ответ API и сохраняет серверный порядок по оценке времени.

            Отклоняет отсутствие пути и привязку точки к дороге дальше километра.
            Выбирает первый вариант; устаревший ответ игнорирует.
            """
            if token != self.sequence:
                return
            if result.get('code') != 'Ok' or not result.get('routes'):
                failed('Между точками не найден автомобильный путь')
                return
            waypoints = result.get('waypoints', [])
            if any(w['distance'] > 1000 for w in waypoints):
                failed('Точка слишком далеко от автомобильной дороги. Выберите точку ближе к дороге')
                return
            self.routes = result['routes']
            self.active_route = 0
            self.draw_routes(True)
            note = ' Начало и конец привязаны к ближайшим дорогам.' if any(w['distance'] > 50 for w in waypoints) else ''
            status(f'Сравнили вариантов: {len(self.routes)}. Выбран лучший по расчётному времени среди полученных.' + note)
            if len(self.routes) == 1:
                status('Отдельный объезд не найден. Показан единственный полученный путь; синий контур не означает отсутствие затора.')
        coords = ';'.join(f"{self.points[k]['lng']},{self.points[k]['lat']}" for k in ('a', 'b'))
        start, end = document['time-from'].value, document['time-to'].value
        if not start or not end or start > end:
            failed('Укажите корректный интервал времени')
            return
        query = '&dataset=' + document['dataset'].value + '&from=' + start + '&to=' + end
        self.request = get_json('/api/route?points=' + window.encodeURIComponent(coords) + query, received, failed)

    def filters_changed(self, event):
        """Обновляет статистику и заново сравнивает маршруты при смене фильтров."""
        self.render_detectors(event)
        self.schedule()

    def choose_route(self, index, fit=False):
        """Выбирает маршрут по index и перерисовывает варианты.

        При fit=True также подстраивает масштаб карты под выбранный путь.
        """
        self.active_route = index
        self.draw_routes(fit)

    def draw_routes(self, fit=False):
        """Рисует линии маршрутов и кнопки с длиной и расчётным временем.

        Активный путь рисует последним поверх альтернатив. fit управляет
        подстройкой границ карты; вызывается при непустом списке routes.
        """
        self.route_layer.clearLayers()
        document['routes'].clear()
        order = [i for i in range(len(self.routes)) if i != self.active_route] + [self.active_route]
        for i in order:
            active = i == self.active_route
            congested = any(s['level'] in ('busy', 'jam') for s in self.routes[i].get('traffic', {}).get('sections', []))
            if i == 0:
                self.L.geoJSON(js(self.routes[i]['geometry']), js({'style': {
                    'color': '#2563eb', 'weight': 13, 'opacity': .55, 'interactive': False,
                }})).addTo(self.route_layer)
            color = '#e04444' if congested else '#2563eb' if i == 0 else '#7c3aed'
            line = self.L.geoJSON(js(self.routes[i]['geometry']), js({'style': {'color': color, 'weight': 8 if active else 6, 'opacity': 0.95 if active else 0.85}})).addTo(self.route_layer)
            def clicked(event, index=i):
                """Выбирает маршрут index и останавливает всплытие клика к карте."""
                self.L.DomEvent.stopPropagation(event)
                self.choose_route(index)
            line.on('click', clicked)
        for i, route in enumerate(self.routes):
            button = html.BUTTON(type='button', Class='route-card' + (' chosen' if i == self.active_route else ''))
            button.setAttribute('aria-pressed', str(i == self.active_route).lower())
            label = 'Быстрее с учётом пробок' if route.get('routeRole') == 'detour' else 'Альтернатива · медленнее' if route.get('routeRole') == 'alternative' else 'Кратчайший из найденных' if route.get('routeRole') == 'original' else 'Лучший по оценке времени' if i == 0 else 'Исходный путь'
            button <= html.STRONG(label)
            traffic = route.get('traffic', {})
            minutes = max(1, math.floor(traffic.get('duration', route['duration']) / 60 + 0.5))
            button <= html.SPAN(f"{route['distance'] / 1000:.1f} км · ≈ {minutes} мин")
            if route.get('routeRole') == 'detour':
                original = next(r for r in self.routes if r.get('routeRole') == 'original')
                saved = (original['traffic']['duration'] - traffic['duration']) / 60
                button <= html.SMALL(f'Экономия времени: ≈ {saved:.1f} мин')
            if route.get('routeRole') == 'alternative':
                button <= html.SMALL('Объезд найден, но выигрыша по расчётному времени нет')
            button <= html.SMALL(f"Камер: {len(traffic.get('cameras', []))} · покрытие данными: {traffic.get('coverage', 0)}%")
            congested = any(s['level'] in ('busy', 'jam') for s in traffic.get('sections', []))
            if congested:
                button <= html.SMALL('Через загруженный участок · красный путь')
            button.bind('click', lambda event, index=i: self.choose_route(index, True))
            document['routes'] <= button
        self.draw_route_traffic()
        if fit:
            bounds = self.L.geoJSON(js(self.routes[self.active_route]['geometry'])).getBounds()
            for route in self.routes:
                bounds.extend(self.L.geoJSON(js(route['geometry'])).getBounds())
            self.map.fitBounds(bounds, js({'padding': [45, 45], 'maxZoom': 16}))

    def draw_route_traffic(self):
        """Показывает оценённые участки выбранного маршрута и список камер.

        Загруженные участки красные; свободные зелёные, неизвестные серые.
        Привязка камер к линии приблизительная, без определения направления.
        """
        self.route_traffic.clearLayers()
        traffic = self.routes[self.active_route].get('traffic', {})
        for route in self.routes:
            for section in route.get('traffic', {}).get('sections', []):
                if section['level'] in ('busy', 'jam'):
                    self.L.polyline(js([[p[1], p[0]] for p in section['geometry']]), js({
                        'pane': 'route-traffic', 'color': '#e04444', 'weight': 10,
                        'opacity': 1, 'interactive': False,
                    })).addTo(self.route_traffic)
        colors = dict(COLORS, busy='#e04444', unknown='#94a3b8')
        for section in traffic.get('sections', []):
            level = section['level']
            self.L.polyline(js([[p[1], p[0]] for p in section['geometry']]), js({
                'pane': 'route-traffic', 'color': colors[level],
                'weight': 10 if level == 'jam' else 8 if level == 'busy' else 4,
                'opacity': 1, 'interactive': False, 'lineCap': 'round',
                'dashArray': '5 7' if level == 'unknown' else None,
            })).addTo(self.route_traffic)
        panel = document['route-traffic']
        panel.clear()
        panel <= html.P('Синий контур — лучший по расчётному времени путь. Красный путь — вариант с загруженным участком; красным показаны заторы всех вариантов, даже при выборе объезда. Наличие затора на части пути не означает, что весь путь загружен.', Class='fine')
        source = f"Выборка {int(document['dataset'].value) + 1} за 15.10.2025, {document['time-from'].value}–{document['time-to'].value}"
        panel <= html.P(f"{source}. Покрытие: {traffic.get('coverage', 0)}% пути. Дополнительное время: ≈ {traffic.get('delay', 0) / 60:.1f} мин.", Class='fine')
        if any(s.get('source') == 'single-camera' for s in traffic.get('sections', [])):
            panel <= html.P('Оценка по одной камере: скорость приблизительно распространяется на 150 м дороги в каждую сторону от камеры. Точная длина затора неизвестна.', Class='fine')
        panel <= html.P('Порог модели: от 20 проездов за выбранный интервал — условный затор для всех камер; время участка увеличивается минимум втрое. Красная линия — загруженный участок выбранного маршрута; более толстая — сильное замедление. Зелёный — небольшое замедление; серый пунктир — нет оценки. Привязка камер в пределах 35 м приблизительная, направление неизвестно.', Class='fine')
        if not any(s['level'] in ('busy', 'jam') for s in traffic.get('sections', [])):
            panel <= html.P('На участках с данными заметное замедление не обнаружено. Неизвестные участки не считаются свободными.', Class='fine')
        for camera in traffic.get('cameras', []):
            speed = f"{camera['speed']:.1f} км/ч" if camera.get('speed') is not None else 'нет наблюдений'
            panel <= html.P(f"{camera['name']} · {speed} · измерений: {camera['samples']}", Class='fine')
        if not traffic.get('cameras'):
            panel <= html.P('Камеры рядом с маршрутом не найдены. Используется базовая оценка OSRM; отсутствие камер не означает свободную дорогу.', Class='fine')

    def swap(self, event):
        """Меняет А и Б местами, обновляет маркеры и пересчитывает маршрут.

        event — событие кнопки; при отсутствии одной из точек ничего не делает.
        """
        if not all(self.points.values()):
            return
        self.points['a'], self.points['b'] = self.points['b'], self.points['a']
        for key in ('a', 'b'):
            self.marker(key)
        self.selected = None
        self.update()
        self.schedule()

    def reset(self, event):
        """Удаляет точки и маршруты и возвращает интерфейс к выбору А.

        event — событие кнопки сброса.
        """
        self.invalidate()
        for key in ('a', 'b'):
            if self.markers[key] is not None:
                self.map.removeLayer(self.markers[key])
            self.markers[key] = self.points[key] = None
        document['routes'].text = 'Выберите А и Б. Построим автомобильные маршруты по дорогам.'
        self.select('a')

    def focus(self, layer, coordinates=None):
        """Включает нужный слой, приближает объект layer и открывает подсказку.

        Если переданы coordinates, показывает камеру с масштабом 16;
        иначе подстраивает границы карты под линию дорожного участка.
        """
        if not document['show-cameras' if coordinates else 'show-traffic'].checked:
            document['show-cameras' if coordinates else 'show-traffic'].checked = True
            self.map.addLayer(self.cameras if coordinates else self.traffic)
        if coordinates:
            self.map.setView(js(coordinates), 16)
        else:
            self.map.fitBounds(layer.getBounds(), js({'padding': [40, 40], 'maxZoom': 16}))
        layer.openPopup()

    def render_data(self):
        """Перерисовывает дороги, подписи и импортированные камеры из self.data.

        Обновляет пометку демоданных и пояснение источника. Исходные детекторы
        рисует отдельно render_detectors с учётом фильтра времени.
        """
        self.traffic.clearLayers()
        self.cameras.clearLayers()
        document['roads'].clear()
        document['data-badge'].text = 'ДЕМО' if self.data['demo'] else 'ЗАГРУЖЕНО'
        document['data-note'].text = ('Условные участки и камеры для демонстрации. Геометрия не привязана к дорогам; это не текущие пробки.' if self.data['demo'] else 'Координаты детекторов из таблицы. Дорожные участки пока не предоставлены. При загрузке JSON геометрия определяется вашим файлом.')
        for road in self.data['roads']:
            line = self.L.polyline(js(road['coordinates']), js({'pane': 'traffic', 'color': COLORS[road['level']], 'weight': 6, 'opacity': 0.85})).addTo(self.traffic).bindPopup(popup(road['name'], LEVELS[road['level']]))
            row = html.BUTTON(type='button', Class='road-row')
            dot = html.I(Class='dot')
            dot.style.background = COLORS[road['level']]
            row <= dot
            row <= html.SPAN(road['name'])
            row <= html.SMALL(LEVELS[road['level']])
            row.bind('click', lambda event, line=line: self.focus(line))
            document['roads'] <= row
        if self.imported:
            for camera in self.data['cameras']:
                icon = self.L.divIcon(js({'className': '', 'html': '<div class="camera-marker">●</div>', 'iconSize': [26, 26], 'iconAnchor': [13, 13]}))
                detail = ', '.join(str(c) for c in camera['coordinates']) + (' · демонстрационная камера' if self.data['demo'] else '')
                self.L.marker(js(camera['coordinates']), js({'title': camera['name'], 'alt': camera['name']})).setIcon(icon).addTo(self.cameras).bindPopup(popup(camera['name'], detail))

    def import_data(self, event):
        """Читает выбранный JSON-файл и запускает проверку его содержимого.

        event — событие выбора файла. Ограничивает размер 5 МБ, использует
        асинхронный FileReader; данные хранятся до перезагрузки страницы.
        """
        files = document['import-data'].files
        if not len(files):
            return
        file = files[0]
        def failed(message):
            """Показывает ошибку импорта и очищает поле, позволяя выбрать файл снова."""
            document['data-status'].text = 'Не удалось загрузить: ' + message
            document['import-data'].value = ''
        if file.size > 5000000:
            failed('Файл должен быть меньше 5 МБ')
            return
        reader = window.FileReader.new()
        def loaded(event):
            """Проверяет прочитанный JSON и заменяет отображаемые дороги и камеры.

            При успехе отменяет актуальность запросов счётчиков и очищает список
            детекторов. При ошибке разбора или проверки сохраняет прежние данные.
            """
            try:
                data = validate(json.loads(str(reader.result)))
            except Exception as error:
                failed(str(error))
                return
            self.data = data
            self.imported = True
            self.detector_sequence += 1
            document['detector-list'].clear()
            document['detector-summary'].text = 'Показаны камеры из JSON. Статистика детекторов доступна после перезагрузки страницы.'
            self.render_data()
            document['data-status'].text = f"Загружено: {len(data['roads'])} участков, {len(data['cameras'])} камер. Данные действуют до перезагрузки страницы."
            document['import-data'].value = ''
        reader.onload = loaded
        reader.onerror = lambda event: failed('Не удалось прочитать файл')
        reader.readAsText(file)

    def render_detectors(self, event=None):
        """Запрашивает счётчики для выбранной выборки и интервала времени.

        event может отсутствовать при первом запуске. Для импортированных
        камер запрос не выполняет; номера запросов защищают от старых ответов.
        """
        self.detector_sequence += 1
        token = self.detector_sequence
        if self.imported:
            return
        self.cameras.clearLayers()
        document['detector-list'].clear()
        start, end = document['time-from'].value, document['time-to'].value
        if not start or not end or start > end:
            document['detector-summary'].text = 'Укажите интервал внутри одного дня: начало не позже конца.'
            return
        dataset_id = document['dataset'].value
        dataset = self.bootstrap['datasets'][int(dataset_id)]
        document['detector-summary'].text = 'Считаем проезды…'
        def failed(message):
            """Показывает ошибку статистики только для последнего запроса детекторов."""
            if token == self.detector_sequence:
                document['detector-summary'].text = 'Не удалось получить статистику: ' + message
        def received(result):
            """Показывает итоговые счётчики, маркеры и список детекторов.

            Сортирует детекторы по числу проездов; кнопки приближают нужный маркер.
            Устаревшие ответы и ответы после импорта камер игнорирует.
            """
            if token != self.detector_sequence or self.imported:
                return
            counts = result['counts']
            document['detector-summary'].text = f"{result['total']} проездов · {result['vehicles']} машин · замечаний импорта: {len(dataset['issues'])}"
            for sensor in sorted(self.bootstrap['sensors'], key=lambda s: -counts.get(s['name'], 0)):
                name = sensor['name']
                count = counts.get(name, 0)
                icon = self.L.divIcon(js({'className': '', 'html': f'<div class="detector-marker"><b>{name}</b><span>{count}</span></div>', 'iconSize': [48, 42], 'iconAnchor': [24, 21]}))
                marker = self.L.marker(js(sensor['coordinates']), js({'title': f'{name}: {count} проездов', 'alt': name})).setIcon(icon).addTo(self.cameras).bindPopup(popup(name, f"{count} проездов · {start}–{end} · {dataset['name']} · 15.10.2025"))
                button = html.BUTTON(f'{name} · {count} проездов', type='button', Class='road-row')
                button.bind('click', lambda event, marker=marker, coord=sensor['coordinates']: self.focus(marker, coord))
                document['detector-list'] <= button
        get_json(f'/api/detectors?dataset={dataset_id}&from={start}&to={end}', received, failed)


def start():
    """Проверяет загрузку Leaflet и запрашивает начальные данные приложения.

    При ошибке показывает сообщение; при успехе создаёт MapApplication.
    """
    if not hasattr(window, 'L'):
        status('Не удалось загрузить карту. Проверьте интернет и обновите страницу.')
        return
    def loaded(data):
        """Создаёт приложение из данных API и сохраняет ссылку в application."""
        global application
        application = MapApplication(data)
    get_json('/api/bootstrap', loaded, lambda message: status('Не удалось загрузить данные сервера: ' + message))


start()
