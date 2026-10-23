"""Отображение маршрутов в браузере. Здесь развиваются панорамы и визуализация.

Сервер передаёт готовые маршруты в порядке рекомендации; этот модуль
не пересчитывает и не сортирует их. Контракт: ROUTE_API.md.
"""
import json
import math
from browser import document, window, html


def js(value):
    """Преобразует вложенные Python-данные в объекты JavaScript через JSON.

    Нужно для передачи списков, словарей и настроек в Leaflet.
    Возвращает объект JavaScript; value должен быть сериализуемым в JSON.
    """
    return window.JSON.parse(json.dumps(value))


COLORS = {'free': '#16a36a', 'busy': '#e9a323', 'jam': '#e04444'}
LEVELS = {'free': 'Свободно', 'busy': 'Плотное движение', 'jam': 'Пробка'}


class RouteView:
    """Визуальная часть MapApplication.

    Использует map, L, routes, active_route, route_layer, route_traffic.
    routes — готовый ответ API; active_route — выбор пользователя.
    Для панорамы геометрия: self.routes[self.active_route]['geometry'].
    """
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

